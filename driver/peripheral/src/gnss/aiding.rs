//! Small receiver initialization inputs. The caller supplies one monotonic
//! microsecond timebase for observations and progress; values cannot cross a
//! reboot or an unaccounted suspend. Source selection/provenance stays with the
//! caller. A submitted estimate is not an independent receiver observation.
use core::time::Duration;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Capabilities {
    pub time: bool,
    /// Three-dimensional WGS84 position, including ellipsoid altitude.
    pub position: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SubmitError {
    Unsupported,
    Busy,
    InvalidInput,
    Expired,
    Stopped,
    Io,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rejection {
    NoTime,
    Unsupported,
    InvalidData,
    Storage,
    NotReady,
}

/// Delivery evidence, never proof of improved navigation or independent time.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    Accepted,
    /// Complete transport write without an unambiguous receiver acknowledgement.
    Unconfirmed,
    Rejected(Rejection),
    TimedOut,
    Cancelled,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Progress {
    Idle,
    Writing,
    AwaitingAck,
    Complete(Result<Outcome, SubmitError>),
}

/// UTC at `observed_at_us`, with an error bound including source/receipt error.
/// The adapter may conservatively overstate this bound as receiver accuracy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TimeAid {
    pub unix_time_ms: i64,
    pub observed_at_us: u64,
    pub uncertainty: Duration,
    pub max_age: Duration,
    /// Maximum additional clock error per million elapsed microseconds.
    pub drift_ppm: u32,
}

impl TimeAid {
    /// Advance UTC and widen uncertainty, rounding discarded fractions outward.
    /// The receiver adapter adds its own delivery/encoding uncertainty.
    pub fn at(self, now_us: u64) -> Result<(i64, Duration), SubmitError> {
        let elapsed = age(self.observed_at_us, self.max_age, now_us)?;
        let utc = self
            .unix_time_ms
            .checked_add(
                (elapsed / 1_000)
                    .try_into()
                    .map_err(|_| SubmitError::InvalidInput)?,
            )
            .ok_or(SubmitError::InvalidInput)?;
        let drift_ns = (u128::from(elapsed) * u128::from(self.drift_ppm)).div_ceil(1_000);
        let drift = Duration::new(
            (drift_ns / 1_000_000_000)
                .try_into()
                .map_err(|_| SubmitError::InvalidInput)?,
            (drift_ns % 1_000_000_000) as u32,
        );
        let uncertainty = self
            .uncertainty
            .checked_add(drift)
            .and_then(|value| value.checked_add(Duration::from_micros(elapsed % 1_000)))
            .ok_or(SubmitError::InvalidInput)?;
        Ok((utc, uncertainty))
    }
}

/// A coarse 3D position at `observed_at_us`. Altitude is above the WGS84
/// ellipsoid, not mean sea level. Supply a conservative 3D standard deviation;
/// source confidence radii require a documented conversion by the caller.
/// `max_speed_mps` bounds displacement, including vertical motion, after the
/// observation. Zero explicitly asserts stationary operation through max_age.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PositionAid {
    pub latitude_deg: f64,
    pub longitude_deg: f64,
    pub altitude_m: f64,
    pub accuracy_m: f32,
    pub observed_at_us: u64,
    pub max_age: Duration,
    pub max_speed_mps: f32,
}

impl PositionAid {
    pub fn accuracy_at(self, now_us: u64) -> Result<f64, SubmitError> {
        let elapsed = age(self.observed_at_us, self.max_age, now_us)?;
        if !self.latitude_deg.is_finite()
            || !(-90.0..=90.0).contains(&self.latitude_deg)
            || !self.longitude_deg.is_finite()
            || !(-180.0..=180.0).contains(&self.longitude_deg)
            || !self.altitude_m.is_finite()
            || !self.accuracy_m.is_finite()
            || self.accuracy_m <= 0.0
            || !self.max_speed_mps.is_finite()
            || self.max_speed_mps < 0.0
        {
            return Err(SubmitError::InvalidInput);
        }
        Ok(f64::from(self.accuracy_m)
            + f64::from(self.max_speed_mps) * elapsed as f64 / 1_000_000.0)
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Aid {
    Time(TimeAid),
    Position(PositionAid),
}

impl Aid {
    pub fn validate(self, now_us: u64) -> Result<(), SubmitError> {
        match self {
            Self::Time(value) => value.at(now_us).map(|_| ()),
            Self::Position(value) => value.accuracy_at(now_us).map(|_| ()),
        }
    }
}

fn age(observed_at_us: u64, max_age: Duration, now_us: u64) -> Result<u64, SubmitError> {
    if max_age.is_zero() {
        return Err(SubmitError::InvalidInput);
    }
    let elapsed = now_us
        .checked_sub(observed_at_us)
        .ok_or(SubmitError::InvalidInput)?;
    if Duration::from_micros(elapsed) > max_age {
        return Err(SubmitError::Expired);
    }
    Ok(elapsed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn time_preserves_epoch_and_bounds_holdover_and_quantization() {
        let estimate = TimeAid {
            unix_time_ms: 1_800_000_000_000,
            observed_at_us: 1_000_000,
            uncertainty: Duration::from_millis(10),
            max_age: Duration::from_secs(60),
            drift_ppm: 20,
        };
        let (utc, error) = estimate.at(11_000_001).unwrap();
        assert_eq!(utc, 1_800_000_010_000);
        assert_eq!(error, Duration::from_nanos(10_201_001));
        assert_eq!(estimate.at(999_999), Err(SubmitError::InvalidInput));
        assert_eq!(estimate.at(61_000_001), Err(SubmitError::Expired));
        assert_eq!(
            TimeAid {
                unix_time_ms: i64::MAX,
                ..estimate
            }
            .at(1_001_000),
            Err(SubmitError::InvalidInput)
        );
    }

    #[test]
    fn old_positions_widen_and_invalid_coordinates_never_become_seeds() {
        let estimate = PositionAid {
            latitude_deg: 43.4,
            longitude_deg: -79.7,
            altitude_m: 120.0,
            accuracy_m: 10.0,
            observed_at_us: 1_000,
            max_age: Duration::from_secs(30),
            max_speed_mps: 40.0,
        };
        assert_eq!(estimate.accuracy_at(10_001_000), Ok(410.0));
        assert_eq!(estimate.accuracy_at(30_001_001), Err(SubmitError::Expired));
        assert_eq!(
            PositionAid {
                altitude_m: f64::NAN,
                ..estimate
            }
            .accuracy_at(1_000),
            Err(SubmitError::InvalidInput)
        );
        assert_eq!(
            PositionAid {
                max_speed_mps: -1.0,
                ..estimate
            }
            .accuracy_at(1_000),
            Err(SubmitError::InvalidInput)
        );
    }
}
