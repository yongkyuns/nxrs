//! Canonical storage remains in the device; a worker borrows it until retirement.
use super::{device, events, session::WorkerSession, trace::Trace};
use crate::{
    instance::{Ownership, Sealed},
    AppliedConfig, Config, Device, DeviceError, Event, Session,
};
use acquisition::readiness;
use peripheral::imu::Imu;
use std::sync::Mutex;

type Construct<S, B> =
    fn(&B, Config, Trace, &'static Ownership, bool) -> Result<device::Device<S>, DeviceError>;

pub(super) struct Instance<S: Imu + Send + 'static, B: Sync + 'static> {
    id: &'static str,
    binding: B,
    stack_bytes: usize,
    owner: Ownership,
    storage: Mutex<Option<device::Device<S>>>,
    construct: Construct<S, B>,
}
impl<S: Imu + Send + 'static, B: Sync + 'static> Instance<S, B> {
    pub(super) const fn new(
        id: &'static str,
        binding: B,
        stack_bytes: usize,
        construct: Construct<S, B>,
    ) -> Self {
        Self {
            id,
            binding,
            stack_bytes,
            owner: Ownership::new(),
            storage: Mutex::new(None),
            construct,
        }
    }

    pub(super) fn start(
        &'static self,
        config: Config,
        trace: Trace,
        sensor_reference: bool,
        sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    ) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError> {
        let device = (self.construct)(&self.binding, config, trace, &self.owner, sensor_reference)?;
        let applied = device.applied_config();
        {
            let mut storage = self.storage.lock().map_err(|_| {
                self.owner.quarantine();
                DeviceError::Io
            })?;
            debug_assert!(storage.is_none());
            *storage = Some(device);
        }
        // Preserve sink-before-owner destruction even if cancellation setup fails.
        let delivery = (sink, Retained(self));
        let cancel = readiness::Cancel::new(events::pipe).map_err(|_| DeviceError::Io)?;
        let wake = cancel.reader.clone();
        WorkerSession::start_guarded(
            self.stack_bytes,
            cancel,
            delivery.0,
            delivery.1,
            move |stop, emit| {
                let mut storage = self.storage.lock().map_err(|_| DeviceError::Io)?;
                storage
                    .as_mut()
                    .ok_or(DeviceError::Io)?
                    .run(stop, &wake, emit)
            },
        )
        .map(|session| (session, applied))
    }
}

// Dropped after the sink and terminal delivery, including spawn failure/panic.
// Cleanup and descriptor destruction precede release of the device's lease.
struct Retained<S: Imu + Send + 'static, B: Sync + 'static>(&'static Instance<S, B>);
impl<S: Imu + Send + 'static, B: Sync + 'static> Drop for Retained<S, B> {
    fn drop(&mut self) {
        let mut storage = self.0.storage.lock().unwrap_or_else(|error| {
            self.0.owner.quarantine();
            error.into_inner()
        });
        drop(storage.take());
    }
}
impl<S: Imu + Send + 'static, B: Sync + 'static> Sealed for Instance<S, B> {}
impl<S: Imu + Send + 'static, B: Sync + 'static> Device for Instance<S, B> {
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
        self.start(config, Trace::default(), false, sink)
    }
}
