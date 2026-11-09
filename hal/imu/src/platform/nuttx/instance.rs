//! Canonical storage remains in the device; a worker borrows it until retirement.
use super::device_state::DeviceState;
use super::{device, events, factory, trace::Trace};
use crate::{api::Sealed, AppliedConfig, Config, Device, DeviceError, Event, Session};
use acquisition::{readiness, Worker};
use peripheral::imu::{Error, Imu};
use std::sync::Mutex;

/// Canonical storage for platform composition. Declare each physical endpoint
/// once, after profile validation; aliases must share that same static instance.
/// Applications acquire devices through their catalogue, never construct them.
pub struct Instance<S: Imu + Send + 'static, R: 'static> {
    id: &'static str,
    irq_path: Option<&'static str>,
    stack_bytes: usize,
    state: DeviceState,
    storage: Mutex<Option<device::Device<S>>>,
    open_resources: fn(Trace) -> Result<R, DeviceError>,
    initialize: fn(R, S::Settings) -> Result<S, Error>,
}
impl<S: Imu + Send + 'static, R: 'static> Instance<S, R> {
    pub const fn new(
        id: &'static str,
        irq_path: Option<&'static str>,
        stack_bytes: usize,
        open_resources: fn(Trace) -> Result<R, DeviceError>,
        initialize: fn(R, S::Settings) -> Result<S, Error>,
    ) -> Self {
        Self {
            id,
            irq_path,
            stack_bytes,
            state: DeviceState::new(),
            storage: Mutex::new(None),
            open_resources,
            initialize,
        }
    }

    pub fn start(
        &'static self,
        config: Config,
        trace: Trace,
        prepare: impl FnOnce(&mut S) -> Result<(), Error>,
        sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    ) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError> {
        let observed = trace.clone();
        let device = factory::acquire(
            config,
            self.irq_path,
            trace,
            &self.state,
            || (self.open_resources)(observed),
            self.initialize,
        )?;
        let device = device.prepare(prepare)?;
        let applied = device.applied_config();
        {
            let mut storage = self.storage.lock().map_err(|_| {
                self.state.mark_faulted();
                DeviceError::Io
            })?;
            debug_assert!(storage.is_none());
            *storage = Some(device);
        }
        // Destroy the sink before device cleanup, even if cancellation setup fails.
        let delivery = (sink, Retained(self));
        let cancel = readiness::Cancel::new(events::pipe).map_err(|_| DeviceError::Io)?;
        let wake = cancel.reader.clone();
        Worker::start_guarded(
            "imu-acquire",
            self.stack_bytes,
            cancel,
            delivery.0,
            delivery.1,
            Event::Failed,
            move |stop, emit| {
                let mut storage = self.storage.lock().map_err(|_| DeviceError::Io)?;
                storage
                    .as_mut()
                    .ok_or(DeviceError::Io)?
                    .run(stop, &wake, emit)
            },
        )
        .map(|session| (session, applied))
        .map_err(|_| DeviceError::Io)
    }
}

// Dropped after the sink and terminal delivery, including spawn failure/panic.
// Cleanup and descriptor destruction finish before the device becomes available.
struct Retained<S: Imu + Send + 'static, R: 'static>(&'static Instance<S, R>);
impl<S: Imu + Send + 'static, R: 'static> Drop for Retained<S, R> {
    fn drop(&mut self) {
        let mut storage = self.0.storage.lock().unwrap_or_else(|error| {
            self.0.state.mark_faulted();
            error.into_inner()
        });
        drop(storage.take());
    }
}
impl<S: Imu + Send + 'static, R: 'static> Sealed for Instance<S, R> {}
impl<S: Imu + Send + 'static, R: 'static> Device for Instance<S, R> {
    fn id(&self) -> &'static str {
        self.id
    }
    fn default_config(&self) -> Result<Config, DeviceError> {
        Ok(S::DEFAULT)
    }
    fn resolve(&self, config: Config) -> Result<AppliedConfig, DeviceError> {
        device::resolve::<S>(config).map(|(_, applied)| applied)
    }
    fn open<T: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        &'static self,
        config: Config,
        sink: T,
    ) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError> {
        self.start(config, Trace::default(), |_| Ok(()), sink)
    }
}
