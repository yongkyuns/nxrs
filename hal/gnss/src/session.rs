//! Implement GNSS session operations on the shared acquisition worker.
use crate::{DeviceError, Session};
use acquisition::{Wake, Worker};
impl<C: Wake> Session for Worker<C, DeviceError> {
    fn request_stop(&mut self) -> Result<(), DeviceError> {
        Worker::request_stop(self).map_err(|_| DeviceError::Io)
    }

    fn join(&mut self) -> Result<(), DeviceError> {
        self.stop_and_join(DeviceError::Io)
    }

    fn is_finished(&self) -> bool {
        Worker::is_finished(self)
    }

    fn dropped(&self) -> u32 {
        Worker::dropped(self)
    }
}
