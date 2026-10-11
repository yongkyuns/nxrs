//! Frame recording service over a storage capability.
#![no_std]
#![forbid(unsafe_code)]

use storage_api::{DeviceError, Frame, Storage};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Device(DeviceError),
}

impl From<DeviceError> for Error {
    fn from(value: DeviceError) -> Self { Self::Device(value) }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RecordingStats { pub accepted: u64, pub errors: u64 }

pub trait Recordings {
    fn record(&mut self, frame: Frame<'_>) -> Result<(), Error>;
    fn flush(&mut self) -> Result<(), Error>;
}

pub struct RecordingService<D> {
    device: D,
    stats: RecordingStats,
}

impl<D: Storage> RecordingService<D> {
    pub fn new(device: D) -> Self { Self { device, stats: RecordingStats::default() } }
    pub fn stats(&self) -> RecordingStats { self.stats }
    pub fn backend(&self) -> &D { &self.device }
}

impl<D: Storage> Recordings for RecordingService<D> {
    fn record(&mut self, frame: Frame<'_>) -> Result<(), Error> {
        match self.device.append(frame) {
            Ok(()) => {
                self.stats.accepted = self.stats.accepted.saturating_add(1);
                Ok(())
            }
            Err(error) => {
                self.stats.errors = self.stats.errors.saturating_add(1);
                Err(error.into())
            }
        }
    }

    fn flush(&mut self) -> Result<(), Error> { self.device.flush().map_err(Into::into) }
}
