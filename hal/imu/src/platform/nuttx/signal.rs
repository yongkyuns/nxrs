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
        empty_recheck: bool,
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
            Some(irq) => Self::Interrupt {
                irq,
                ready: None,
                empty_recheck: false,
            },
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
            Self::Interrupt { irq, ready, .. } => {
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
            } => loop {
                if !wait_until(cancel, *next_deadline)? {
                    return Ok(None);
                }
                if let Some(deadline) =
                    advance_periodic_deadline(next_deadline, *period, Instant::now())
                {
                    return Ok(Some(Wake {
                        periodic_deadline: Some(deadline),
                    }));
                }
            },
        }
    }
    /// True requests one deferred recheck: data can arrive between an empty
    /// FIFO snapshot and the GPIO read. A still-empty FIFO after a full delivery
    /// interval with the line asserted is an inconsistent interrupt source.
    pub(super) fn drained(&mut self, count: usize) -> Result<bool, DeviceError> {
        if let Self::Interrupt {
            irq,
            ready,
            empty_recheck,
        } = self
        {
            let high = irq.read().map_err(gpio_error)?;
            *ready = Some(high);
            if high && count == 0 {
                if core::mem::replace(empty_recheck, true) {
                    return Err(DeviceError::Io);
                }
                return Ok(true);
            }
            *empty_recheck = false;
        }
        Ok(false)
    }
    /// An acknowledged watermark may remain high, so an ambiguous snapshot or
    /// empty/high race cannot depend on a new edge. Retry at the normal delivery
    /// cadence, with cancellation still able to interrupt the wait.
    pub(super) fn retry(&mut self, cancel: &File, interval: Duration) -> Result<bool, DeviceError> {
        let Self::Interrupt { ready, .. } = self else {
            return Ok(true);
        };
        if !wait_until(cancel, Instant::now() + interval)? {
            return Ok(false);
        }
        *ready = Some(true);
        Ok(true)
    }
    pub(super) fn close(&mut self) -> Result<(), DeviceError> {
        match self {
            Self::Interrupt { irq, .. } => irq.unregister().map_err(gpio_error),
            Self::Periodic { .. } => Ok(()),
        }
    }
}

fn wait_until(cancel: &File, deadline: Instant) -> Result<bool, DeviceError> {
    loop {
        let timeout = deadline
            .saturating_duration_since(Instant::now())
            .as_nanos()
            .div_ceil(1_000_000)
            .min(i32::MAX as u128) as i32;
        match events::tick(cancel.as_raw_fd(), timeout) {
            Ok(false) => return Ok(false),
            Ok(true) if Instant::now() >= deadline => return Ok(true),
            Ok(true) => {} // Early/spurious wakes retain the absolute deadline.
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
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
