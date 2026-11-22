use crate::{Device, Error, Status};
use peripheral::battery::Gauge;
use std::sync::{Mutex, TryLockError};

pub struct Instance<G, B> {
    binding: B,
    open: fn(B) -> Result<G, Error>,
    gate: Mutex<()>,
}

impl<G, B> Instance<G, B> {
    pub const fn new(binding: B, open: fn(B) -> Result<G, Error>) -> Self {
        Self {
            binding,
            open,
            gate: Mutex::new(()),
        }
    }
}

impl<G: Gauge, B: Copy> Device for Instance<G, B> {
    fn status(&self) -> Result<Status, Error> {
        let _guard = self.gate.try_lock().map_err(|error| match error {
            TryLockError::WouldBlock => Error::Busy,
            TryLockError::Poisoned(_) => Error::Io,
        })?;
        (self.open)(self.binding)?.status().map_err(|_| Error::Io)
    }
}
