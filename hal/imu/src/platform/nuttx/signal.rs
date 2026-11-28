//! Platform-local wake sources; both feed the same FIFO acquisition loop.
use super::{
    device::{gpio_error, Irq},
    events,
};
use crate::DeviceError;
use acquisition::readiness;
use std::{
    fs::File,
    io::ErrorKind,
    os::fd::AsRawFd,
    time::{Duration, Instant},
};

pub(super) enum Signal {
    Interrupt {
        irq: Irq,
        ready: Option<bool>,
    },
    Periodic {
        period: Duration,
        next_deadline: Instant,
    },
}

pub(super) struct Wake {
    pub(super) periodic_deadline: Option<Instant>,
}

impl Signal {
    pub(super) fn new(irq: Option<Irq>, period: Duration) -> Self {
        match irq {
            Some(irq) => Self::Interrupt { irq, ready: None },
            None => {
                assert!(!period.is_zero());
                Self::Periodic {
                    period,
                    next_deadline: Instant::now() + period,
                }
            }
        }
    }
    pub(super) fn wait(&mut self, cancel: &File) -> Result<Option<Wake>, DeviceError> {
        match self {
            Self::Interrupt { irq, ready } => {
                // Close the sensor-enable/initial-wait race without losing a level.
                if ready.is_none() {
                    *ready = Some(irq.read().map_err(gpio_error)?);
                }
                if !ready.unwrap_or(false)
                    && !readiness::wait(irq.file(), cancel, events::wait)
                        .map_err(|_| DeviceError::Io)?
                {
                    return Ok(None);
                }
                irq.read().map_err(gpio_error)?; // acknowledge the GPIO notification before FIFO I/O
                Ok(Some(Wake {
                    periodic_deadline: None,
                }))
            }
            Self::Periodic {
                period,
                next_deadline,
            } => {
                let Some(now) = wait_until(cancel, *next_deadline)? else {
                    return Ok(None);
                };
                Ok(Some(Wake {
                    periodic_deadline: advance_periodic_deadline(next_deadline, *period, now),
                }))
            }
        }
    }

    pub(super) fn retry(&mut self, cancel: &File, interval: Duration) -> Result<bool, DeviceError> {
        let Self::Interrupt { ready, .. } = self else {
            // Periodic acquisition already waits for the next scheduled drain.
            return Ok(true);
        };
        // The FIFO may keep the watermark asserted after the edge was acknowledged.
        // Retry at the normal delivery cadence even if another edge never arrives.
        if wait_until(cancel, Instant::now() + interval)?.is_none() {
            return Ok(false);
        }
        *ready = Some(true);
        Ok(true)
    }

    pub(super) fn drained(&mut self, count: usize) -> Result<(), DeviceError> {
        if let Self::Interrupt { irq, ready } = self {
            let high = irq.read().map_err(gpio_error)?;
            *ready = Some(high);
            if high && count == 0 {
                return Err(DeviceError::Io);
            }
        }
        Ok(())
    }

    pub(super) fn close(&mut self) -> Result<(), DeviceError> {
        match self {
            Self::Interrupt { irq, .. } => irq.unregister().map_err(gpio_error),
            Self::Periodic { .. } => Ok(()),
        }
    }
}

/// Ignore early wakes and interruptions while retaining the absolute deadline.
fn wait_until(cancel: &File, deadline: Instant) -> Result<Option<Instant>, DeviceError> {
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let timeout = remaining
            .as_nanos()
            .div_ceil(1_000_000)
            .min(i32::MAX as u128) as i32;
        match events::tick(cancel.as_raw_fd(), timeout) {
            Ok(false) => return Ok(None),
            Ok(true) => {
                let now = Instant::now();
                if now >= deadline {
                    return Ok(Some(now));
                }
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => {}
            Err(_) => return Err(DeviceError::Io),
        }
    }
}

/// Keep early wakes on schedule and skip missed periods instead of accumulating reads.
pub(super) fn advance_periodic_deadline(
    next_deadline: &mut Instant,
    period: Duration,
    now: Instant,
) -> Option<Instant> {
    if now < *next_deadline {
        return None;
    }
    let due = *next_deadline;
    *next_deadline += period;
    if *next_deadline <= now {
        *next_deadline = now + period;
    }
    Some(due)
}
