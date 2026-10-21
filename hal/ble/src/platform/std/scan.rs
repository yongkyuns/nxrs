//! Private Rust scan contract; SDK field translation stays in each backend.
use crate::{Error, Uuid};
pub(crate) type Report = crate::worker::ScanReport<[u8; 16]>;
pub(crate) trait ScanBackend: Send + 'static + Sized {
    fn open() -> Result<Self, Error>;
    fn start(&mut self, service: Option<Uuid>) -> Result<(), Error>;
    fn get(&mut self, output: &mut [Report]) -> Result<usize, Error>;
    fn stop(&mut self) -> Result<(), Error>;
    fn close(&mut self);
}
