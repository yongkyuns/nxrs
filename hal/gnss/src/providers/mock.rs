//! Deterministic synthetic GNSS for architecture demonstrations.
#![forbid(unsafe_code)]

use crate::{DeviceError, Gnss, GnssFix};

#[derive(Default)]
pub struct SyntheticGnss {
    sequence: u64,
    north_m: f32,
    east_m: f32,
}

impl Gnss for SyntheticGnss {
    fn fix(&mut self, now_ms: u64) -> Result<GnssFix, DeviceError> {
        self.sequence = self.sequence.saturating_add(1);
        self.north_m += 0.05;
        self.east_m += 0.80;
        Ok(GnssFix {
            sequence: self.sequence,
            timestamp_ms: now_ms,
            north_m: self.north_m,
            east_m: self.east_m,
            speed_mps: 4.0,
            course_rad: 0.08,
        })
    }
}
