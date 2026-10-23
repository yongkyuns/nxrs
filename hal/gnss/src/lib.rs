//! GNSS contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
mod api;
pub use api::{DeviceError, Gnss, GnssFix};
mod platform;
pub use platform::*;

#[cfg(test)]
mod tests {
    use crate::{platform::mock, Gnss};

    #[test]
    fn synthetic_fixes_have_increasing_sequences_and_caller_timestamps() {
        let mut gnss = mock::SyntheticGnss::default();
        let first = gnss.fix(10).unwrap();
        let second = gnss.fix(20).unwrap();
        assert_eq!((first.sequence, second.sequence), (1, 2));
        assert_eq!((first.timestamp_ms, second.timestamp_ms), (10, 20));
        assert!(second.east_m > first.east_m);
    }
}
