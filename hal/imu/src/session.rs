//! Task-context ownership for a normalized IMU event source.
use crate::{DeviceError, Event, Session};
use acquisition::{Wake, Worker};
use core::sync::atomic::AtomicBool;

pub(crate) struct WorkerSession<C: Wake>(Worker<C, DeviceError>);

impl<C: Wake> WorkerSession<C> {
    #[allow(
        dead_code,
        reason = "Providers choose ordinary or retained-instance execution."
    )]
    pub(crate) fn start<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        stack_bytes: usize,
        cancel: C,
        sink: S,
        run: impl FnOnce(&AtomicBool, &mut dyn FnMut(Event) -> bool) -> Result<(), DeviceError>
            + Send
            + 'static,
    ) -> Result<Self, DeviceError> {
        Self::start_guarded(stack_bytes, cancel, sink, (), run)
    }

    pub(crate) fn start_guarded<
        S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
        G: Send + 'static,
    >(
        stack_bytes: usize,
        cancel: C,
        sink: S,
        guard: G,
        run: impl FnOnce(&AtomicBool, &mut dyn FnMut(Event) -> bool) -> Result<(), DeviceError>
            + Send
            + 'static,
    ) -> Result<Self, DeviceError> {
        Worker::start_guarded(
            "imu-acquire",
            stack_bytes,
            cancel,
            sink,
            guard,
            Event::Failed,
            run,
        )
        .map(Self)
        .map_err(|_| DeviceError::Io)
    }
}

impl<C: Wake> Session for WorkerSession<C> {
    fn request_stop(&mut self) -> Result<(), DeviceError> {
        self.0.request_stop().map_err(|_| DeviceError::Io)
    }

    fn join(&mut self) -> Result<(), DeviceError> {
        self.0.stop_and_join(DeviceError::Io)
    }

    fn is_finished(&self) -> bool {
        self.0.is_finished()
    }

    fn dropped(&self) -> u32 {
        self.0.dropped()
    }
}
