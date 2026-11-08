//! Task-context ownership for a normalized GNSS event source.
use crate::{DeviceError, Event, Session};
use acquisition::{Wake, Worker};
use core::sync::atomic::AtomicBool;

pub(crate) struct WorkerSession<C: Wake>(Worker<C, DeviceError>);

impl<C: Wake> WorkerSession<C> {
    pub(crate) fn start<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        stack_bytes: usize,
        cancel: C,
        sink: S,
        run: impl FnOnce(&AtomicBool, &mut dyn FnMut(Event) -> bool) -> Result<(), DeviceError>
            + Send
            + 'static,
    ) -> Result<Self, DeviceError> {
        Worker::start(
            "gnss-acquire",
            stack_bytes,
            cancel,
            sink,
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
        self.request_stop()?;
        self.0.join(DeviceError::Io)
    }

    fn is_finished(&self) -> bool {
        self.0.is_finished()
    }

    fn dropped(&self) -> u32 {
        self.0.dropped()
    }
}
