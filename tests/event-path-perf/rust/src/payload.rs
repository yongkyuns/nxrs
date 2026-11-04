//! Checked payloads shared by the matched transport cases.
//!
//! The normalized sensor case uses the real HAL contract. No hardware provider
//! is acquired; every byte of its meaningful fields participates in validation.
#![forbid(unsafe_code)]

use std::fmt::Debug;

pub trait Payload: Copy + Send + Sync + PartialEq + Debug + 'static {
    const NAME: &'static str;

    fn new(sequence: u32, provider: u32) -> Self;
    fn sequence(self) -> u32;
    fn provider(self) -> u32;
    fn checksum(self) -> u64;

    fn valid(self, sequence: u32, provider: u32) -> bool {
        self == Self::new(sequence, provider)
    }
}

/// The original baseline's payload size, now with a checked provider identity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Bytes16(pub [u32; 4]);

impl Payload for Bytes16 {
    const NAME: &'static str = "bytes16";

    fn new(sequence: u32, provider: u32) -> Self {
        Self([
            sequence,
            provider,
            sequence.rotate_left(7) ^ provider ^ 0x9173_ac5e,
            !sequence ^ provider.rotate_right(3) ^ 0x1357_2468,
        ])
    }

    fn sequence(self) -> u32 {
        self.0[0]
    }

    fn provider(self) -> u32 {
        self.0[1]
    }

    fn checksum(self) -> u64 {
        self.0.into_iter().fold(0_u64, mix)
    }
}

fn mix(checksum: u64, word: u32) -> u64 {
    checksum.rotate_left(11).wrapping_add(u64::from(word))
}

impl Payload for imu::ImuSample {
    const NAME: &'static str = "imu-sample";

    fn new(sequence: u32, provider: u32) -> Self {
        // This is a synthetic normalized sample, not a real acquisition clock.
        // Preserve the complete provider/sequence identity in integer fields;
        // bounded, exactly representable float values keep equality unambiguous.
        Self {
            sequence: u64::from(sequence),
            timestamp_ms: (u64::from(provider) << 32) | u64::from(sequence),
            sample_interval_us: 5_000,
            accel_mps2: [(sequence & 1023) as f32, 0.5, 9.75],
            gyro_rps: [(provider & 1023) as f32, -0.25, 0.125],
        }
    }

    fn sequence(self) -> u32 {
        self.sequence as u32
    }

    fn provider(self) -> u32 {
        (self.timestamp_ms >> 32) as u32
    }

    fn checksum(self) -> u64 {
        // Hash fields, never padding: target layout is recorded by the harness.
        let words = [
            self.sequence as u32,
            (self.sequence >> 32) as u32,
            self.timestamp_ms as u32,
            (self.timestamp_ms >> 32) as u32,
            self.sample_interval_us,
            self.accel_mps2[0].to_bits(),
            self.accel_mps2[1].to_bits(),
            self.accel_mps2[2].to_bits(),
            self.gyro_rps[0].to_bits(),
            self.gyro_rps[1].to_bits(),
            self.gyro_rps[2].to_bits(),
        ];
        words.into_iter().fold(0_u64, mix)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn baseline_has_exactly_sixteen_bytes_and_checks_every_word() {
        assert_eq!(std::mem::size_of::<Bytes16>(), 16);
        assert_eq!(std::mem::align_of::<Bytes16>(), 4);
        let value = Bytes16::new(37, 3);
        assert!(value.valid(37, 3));
        assert!(!value.valid(36, 3));
        assert!(!value.valid(37, 2));
        for index in 0..4 {
            let mut changed = value;
            changed.0[index] ^= 1;
            assert!(!changed.valid(37, 3));
            assert_ne!(value.checksum(), changed.checksum());
        }
    }

    #[test]
    fn real_imu_payload_keeps_integer_identity_and_validates_all_fields() {
        let value = imu::ImuSample::new(u32::MAX, u32::MAX);
        assert_eq!(value.sequence(), u32::MAX);
        assert_eq!(value.provider(), u32::MAX);
        assert!(value.valid(u32::MAX, u32::MAX));
        let mut changed = value;
        changed.sequence ^= 1_u64 << 40;
        assert!(!changed.valid(u32::MAX, u32::MAX));
        assert_ne!(value.checksum(), changed.checksum());
        changed = value;
        changed.timestamp_ms ^= 1;
        assert!(!changed.valid(u32::MAX, u32::MAX));
        assert_ne!(value.checksum(), changed.checksum());
        changed = value;
        changed.sample_interval_us += 1;
        assert!(!changed.valid(u32::MAX, u32::MAX));
        assert_ne!(value.checksum(), changed.checksum());
        for index in 0..3 {
            changed = value;
            changed.accel_mps2[index] += 1.0;
            assert!(!changed.valid(u32::MAX, u32::MAX));
            assert_ne!(value.checksum(), changed.checksum());
            changed = value;
            changed.gyro_rps[index] += 1.0;
            assert!(!changed.valid(u32::MAX, u32::MAX));
            assert_ne!(value.checksum(), changed.checksum());
        }
    }
}
