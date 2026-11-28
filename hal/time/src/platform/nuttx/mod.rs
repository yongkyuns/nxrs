use crate::{Capabilities, Control, Error};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

#[allow(unsafe_code)]
mod ffi;

static TAKEN: AtomicBool = AtomicBool::new(false);
// The repaired software backend represents a correction in signed nanoseconds.
const MAX_ADJUSTMENT_US: i64 = i64::MAX / 1_000;

struct Owner {
    capabilities: Capabilities,
}

impl Drop for Owner {
    fn drop(&mut self) {
        TAKEN.store(false, Ordering::Release);
    }
}

/// Acquire the configured system clock. NuttX RTC_HIRES is unsupported because
/// its monotonic clock is coupled to an RTC that wall-clock setting can change.
pub fn open() -> Result<impl Control, Error> {
    let capabilities = ffi::capabilities()?;
    if TAKEN.swap(true, Ordering::AcqRel) {
        return Err(Error::Busy);
    }
    Ok(Owner { capabilities })
}

impl Control for Owner {
    fn capabilities(&self) -> Capabilities {
        self.capabilities
    }

    fn set(&mut self, time: SystemTime) -> Result<(), Error> {
        let (negative, elapsed) = match time.duration_since(UNIX_EPOCH) {
            Ok(elapsed) => (false, elapsed),
            Err(before) => (true, before.duration()),
        };
        let fraction = elapsed.subsec_nanos();
        let (seconds, nanoseconds) = if negative {
            (
                -i128::from(elapsed.as_secs()) - i128::from(fraction != 0),
                if fraction == 0 {
                    0
                } else {
                    1_000_000_000 - fraction
                },
            )
        } else {
            (i128::from(elapsed.as_secs()), fraction)
        };
        let seconds = i64::try_from(seconds).map_err(|_| Error::OutOfRange)?;
        ffi::set(seconds, nanoseconds)
    }

    fn replace_adjustment(&mut self, correction_us: i64) -> Result<i64, Error> {
        self.require_slew()?;
        if !(-MAX_ADJUSTMENT_US..=MAX_ADJUSTMENT_US).contains(&correction_us) {
            return Err(Error::OutOfRange);
        }
        adjustment(Some(correction_us))
    }

    fn pending_adjustment(&mut self) -> Result<i64, Error> {
        self.require_slew()?;
        adjustment(None)
    }
}

impl Owner {
    fn require_slew(&self) -> Result<(), Error> {
        self.capabilities
            .slew_rate_ppm
            .ok_or(Error::Unsupported)
            .map(|_| ())
    }
}

fn adjustment(correction_us: Option<i64>) -> Result<i64, Error> {
    let requested = correction_us.unwrap_or(0);
    let (seconds, microseconds) = ffi::adjust(
        correction_us.is_some(),
        requested / 1_000_000,
        (requested % 1_000_000) as i32,
    )?;
    // The qualified backend returns normalized fields with the same sign.
    if !(-999_999..=999_999).contains(&microseconds)
        || (seconds > 0 && microseconds < 0)
        || (seconds < 0 && microseconds > 0)
    {
        return Err(Error::Io);
    }
    seconds
        .checked_mul(1_000_000)
        .and_then(|seconds| seconds.checked_add(i64::from(microseconds)))
        .filter(|residual| (-MAX_ADJUSTMENT_US..=MAX_ADJUSTMENT_US).contains(residual))
        .ok_or(Error::Io)
}
