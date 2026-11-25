//! Read-only AXP2101 battery measurements over I²C.
#![forbid(unsafe_code)]

use embedded_hal::i2c::I2c;
use peripheral::battery::{Battery, ChargeState, Gauge, Status};

const PMU_STATUS: u8 = 0x00;
const BATTERY_PRESENT: u8 = 1 << 3;
const INPUT_GOOD: u8 = 1 << 5;
const MODULE_ENABLE: u8 = 0x18;
const GAUGE_ENABLED: u8 = 1 << 3;
const BATTERY_PERCENT: u8 = 0xa4;

pub struct Axp2101<I> {
    bus: I,
    address: u8,
}

impl<I> Axp2101<I> {
    pub fn new(bus: I, address: u8) -> Self {
        Self { bus, address }
    }
}

impl<I: I2c> Axp2101<I> {
    fn read<const N: usize>(&mut self, register: u8) -> Result<[u8; N], I::Error> {
        let mut bytes = [0; N];
        self.bus.write_read(self.address, &[register], &mut bytes)?;
        Ok(bytes)
    }
}

impl<I: I2c> Gauge for Axp2101<I> {
    type Error = I::Error;

    fn status(&mut self) -> Result<Status, Self::Error> {
        let [power, charge] = self.read(PMU_STATUS)?;
        let battery = if power & BATTERY_PRESENT != 0 {
            let [modules] = self.read(MODULE_ENABLE)?;
            let percent = if modules & GAUGE_ENABLED != 0 {
                let [value] = self.read(BATTERY_PERCENT)?;
                (value <= 100).then_some(value)
            } else {
                None
            };
            let charge_state = match (charge >> 5) & 3 {
                0 => ChargeState::Standby,
                1 => ChargeState::Charging,
                2 => ChargeState::Discharging,
                _ => ChargeState::Unknown,
            };
            Some(Battery {
                percent,
                charge_state,
            })
        } else {
            None
        };
        Ok(Status {
            external_power: power & INPUT_GOOD != 0,
            battery,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use embedded_hal::i2c::{ErrorKind, ErrorType, Operation};

    struct Bus<'a>(&'a [(u8, &'a [u8])]);

    impl ErrorType for Bus<'_> {
        type Error = ErrorKind;
    }

    impl I2c for Bus<'_> {
        fn transaction(&mut self, address: u8, ops: &mut [Operation<'_>]) -> Result<(), ErrorKind> {
            assert_eq!(address, 0x34);
            let [Operation::Write(reg), Operation::Read(output)] = ops else {
                panic!("expected register read");
            };
            let ((expected, value), rest) = self.0.split_first().unwrap();
            assert_eq!(*reg, &[*expected]);
            self.0 = rest;
            if value.is_empty() {
                return Err(ErrorKind::Bus);
            }
            output.copy_from_slice(value);
            Ok(())
        }
    }

    #[test]
    fn status_distinguishes_absent_unavailable_and_empty() {
        for (reads, expected) in [
            (&[(0x00, &[0x20, 0][..])][..], (true, None)),
            (
                &[(0x00, &[0x08, 0x40][..]), (0x18, &[0])],
                (false, Some((None, ChargeState::Discharging))),
            ),
            (
                &[(0x00, &[0x28, 0x20][..]), (0x18, &[8]), (0xa4, &[0])],
                (true, Some((Some(0), ChargeState::Charging))),
            ),
            (
                &[(0x00, &[0x28, 0x60][..]), (0x18, &[8]), (0xa4, &[255])],
                (true, Some((None, ChargeState::Unknown))),
            ),
        ] {
            let mut gauge = Axp2101::new(Bus(reads), 0x34);
            let result = gauge.status().unwrap();
            assert_eq!(
                (
                    result.external_power,
                    result.battery.map(|b| (b.percent, b.charge_state))
                ),
                expected
            );
            assert!(gauge.bus.0.is_empty());
        }
    }

    #[test]
    fn preserves_i2c_failures() {
        let reads = [(0x00, &[0x28, 0x20][..]), (0x18, &[8]), (0xa4, &[])];
        assert_eq!(
            Axp2101::new(Bus(&reads), 0x34).status(),
            Err(ErrorKind::Bus)
        );
    }
}
