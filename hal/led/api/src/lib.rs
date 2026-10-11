//! Typed LED capability, without descriptors, paths or native request values.
#![no_std]
#![forbid(unsafe_code)]

pub use hal_common::DeviceError;

/// Supported logical LED indices. Bit n denotes index n, not its current state.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct LedSet(u32);

impl LedSet {
    pub const fn from_bits(bits: u32) -> Self { Self(bits) }
    pub const fn bits(self) -> u32 { self.0 }
    pub const fn contains(self, index: u8) -> bool {
        index < 32 && (self.0 & (1u32 << index)) != 0
    }

    /// Reject out-of-range indices before shifting or entering a driver.
    pub fn validate(self, index: u8) -> Result<(), DeviceError> {
        if index >= 32 {
            Err(DeviceError::InvalidData)
        } else if !self.contains(index) {
            Err(DeviceError::Unsupported)
        } else {
            Ok(())
        }
    }
}

/// Synchronous LED controls. No worker, queue or allocation is required by this
/// contract. Execution time still depends on the selected provider/driver.
/// Dropping a resource does not promise to turn the LEDs off.
pub trait Led {
    fn supported(&self) -> LedSet;
    fn set(&mut self, index: u8, on: bool) -> Result<(), DeviceError>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn masks_preserve_holes_and_high_bit_without_invalid_shifts() {
        let set = LedSet::from_bits(0x8000_0005);
        assert_eq!(set.bits(), 0x8000_0005);
        for index in [0, 2, 31] { assert_eq!(set.validate(index), Ok(())); }
        assert_eq!(set.validate(1), Err(DeviceError::Unsupported));
        for index in [32, 255] {
            assert!(!set.contains(index));
            assert_eq!(set.validate(index), Err(DeviceError::InvalidData));
        }
        assert_eq!(LedSet::default().validate(0), Err(DeviceError::Unsupported));
    }
}
