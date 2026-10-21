//! Allocation-free, independently owned mock LED state.
#![forbid(unsafe_code)]

use crate::{DeviceError, Led, LedSet};

pub struct MockLeds {
    supported: LedSet,
    state: u32,
}

impl MockLeds {
    pub const fn new(supported: LedSet) -> Self {
        Self {
            supported,
            state: 0,
        }
    }
    pub fn state(&self) -> u32 {
        self.state
    }
}

impl Default for MockLeds {
    fn default() -> Self {
        Self::new(LedSet::from_bits(1))
    }
}

impl Led for MockLeds {
    fn supported(&self) -> LedSet {
        self.supported
    }

    fn set(&mut self, index: u8, on: bool) -> Result<(), DeviceError> {
        self.supported.validate(index)?;
        let bit = 1u32 << index;
        if on {
            self.state |= bit;
        } else {
            self.state &= !bit;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn updates_are_independent_and_errors_do_not_change_state() {
        let mut a = MockLeds::new(LedSet::from_bits(0x8000_0005));
        let b = MockLeds::default();
        a.set(31, true).unwrap();
        a.set(0, true).unwrap();
        a.set(0, false).unwrap();
        assert_eq!(a.state(), 0x8000_0000);
        assert_eq!(b.state(), 0);
        assert_eq!(a.set(1, true), Err(DeviceError::Unsupported));
        assert_eq!(a.set(255, true), Err(DeviceError::InvalidData));
        assert_eq!(a.state(), 0x8000_0000);
    }
}
