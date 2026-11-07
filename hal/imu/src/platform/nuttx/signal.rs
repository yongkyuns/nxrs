//! Platform-local wake sources; both feed the same FIFO acquisition loop.
use super::{ffi, irq::Irq, readiness};
use crate::{cadence::Cadence, DeviceError};
use std::{
    fs::File,
    io::ErrorKind,
    os::fd::AsRawFd,
    time::{Duration, Instant},
};

pub(super) enum Signal {
    Interrupt { irq: Irq, ready: Option<bool> },
    Periodic(Cadence),
}

pub(super) struct Wake {
    pub(super) periodic_deadline: Option<Instant>,
}

impl Signal {
    pub(super) fn new(irq: Option<Irq>, period: Duration) -> Self {
        match irq {
            Some(irq) => Self::Interrupt { irq, ready: None },
            None => Self::Periodic(Cadence::new(Instant::now(), period)),
        }
    }
    pub(super) fn wait(&mut self, cancel: &File) -> Result<Option<Wake>, DeviceError> {
        match self {
            Self::Interrupt { irq, ready } => {
                // Close the sensor-enable/initial-wait race without losing a level.
                if ready.is_none() {
                    *ready = Some(irq.read()?);
                }
                if !ready.unwrap_or(false)
                    && !readiness::wait(&irq.file, cancel, ffi::wait)
                        .map_err(|_| DeviceError::Io)?
                {
                    return Ok(None);
                }
                irq.read()?; // acknowledge the GPIO notification before FIFO I/O
                Ok(Some(Wake {
                    periodic_deadline: None,
                }))
            }
            Self::Periodic(clock) => loop {
                let timeout = clock
                    .remaining(Instant::now())
                    .as_nanos()
                    .div_ceil(1_000_000)
                    .min(i32::MAX as u128) as i32;
                match ffi::tick(cancel.as_raw_fd(), timeout) {
                    Ok(false) => return Ok(None),
                    Ok(true) => {
                        // Early or spurious wakes retain the absolute deadline.
                        if let Some(deadline) = clock.take_due(Instant::now()) {
                            return Ok(Some(Wake {
                                periodic_deadline: Some(deadline),
                            }));
                        }
                    }
                    Err(e) if e.kind() == ErrorKind::Interrupted => {}
                    Err(_) => return Err(DeviceError::Io),
                }
            },
        }
    }
    pub(super) fn drained(&mut self, count: usize) -> Result<(), DeviceError> {
        if let Self::Interrupt { irq, ready } = self {
            let high = irq.read()?;
            *ready = Some(high);
            if high && count == 0 {
                return Err(DeviceError::Io);
            }
        }
        Ok(())
    }
    pub(super) fn close(&mut self) -> Result<(), DeviceError> {
        match self {
            Self::Interrupt { irq, .. } => irq.unregister(),
            Self::Periodic(_) => Ok(()),
        }
    }
}
