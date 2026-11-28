//! Validate application-supplied traffic and retention budgets before acquisition.
use messaging::pooled::CAPACITY;
use std::io;

/// Retention to admit before reserving the fixed scalar pool.
///
/// The heap limit covers requested Rust storage for the pool and its control
/// allocation. Allocator bookkeeping, clock/wake state, transport buffers and
/// task stacks remain separate. An admitted arena remains reserved while idle.
#[derive(Clone, Copy, Debug)]
pub struct CaptureBudget {
    pub max_requested_bytes: usize,
    pub stall_ms: u32,
    pub burst_records: u32,
}

impl CaptureBudget {
    fn required_slots(self, rates: [u32; 2], gnss_rate: u32) -> u64 {
        let during_stall =
            |rate: u32| (u64::from(rate) * u64::from(self.stall_ms)).div_ceil(1_000_000);
        during_stall(rates[0])
            + during_stall(rates[1])
            + during_stall(gnss_rate)
            + u64::from(self.burst_records)
    }

    pub(super) fn validate(self, rates: [Option<u32>; 2], gnss_rate: u32) -> io::Result<()> {
        // Failed/unsupported sources have no admitted traffic. Their original
        // acquisition errors remain the application's responsibility.
        if rates.contains(&Some(0)) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "capture requires a nonzero resolved IMU rate",
            ));
        }
        let required = self.required_slots(rates.map(|rate| rate.unwrap_or(0)), gnss_rate);
        if required > CAPACITY as u64 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!(
                    "capture needs {required} slots for a {} ms stall and {} burst records; capacity is {CAPACITY}",
                    self.stall_ms, self.burst_records,
                ),
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const BUDGET: CaptureBudget = CaptureBudget {
        max_requested_bytes: 22 * 1024,
        stall_ms: 2_000,
        burst_records: 64,
    };

    #[test]
    fn supported_rates_fit_the_declared_stall_and_burst() {
        assert_eq!(BUDGET.required_slots([100_000, 100_000], 2_000), 468);
        assert_eq!(BUDGET.required_slots([104_000, 104_000], 2_000), 484);
        BUDGET
            .validate([Some(104_000), Some(104_000)], 2_000)
            .unwrap();
        BUDGET.validate([Some(200_000), None], 2_000).unwrap();
    }

    #[test]
    fn rate_budget_rejects_overcommit_instead_of_reducing_stall_cover() {
        BUDGET
            .validate([Some(111_000), Some(111_000)], 2_000)
            .unwrap();
        assert_eq!(
            BUDGET.required_slots([111_000, 111_000], 2_000),
            CAPACITY as u64
        );
        for rates in [
            [Some(111_001), Some(111_000)],
            [Some(u32::MAX), Some(u32::MAX)],
            [Some(0), None],
        ] {
            assert_eq!(
                BUDGET.validate(rates, 2_000).unwrap_err().kind(),
                io::ErrorKind::InvalidInput
            );
        }
    }

    #[test]
    fn caller_selected_gnss_rate_and_retention_are_accounted() {
        assert_eq!(BUDGET.required_slots([100_000, 100_000], 10_000), 484);
        assert!(BUDGET.validate([Some(111_000); 2], 10_000).is_err());
        let short = CaptureBudget {
            stall_ms: 100,
            burst_records: 0,
            ..BUDGET
        };
        short.validate([Some(1_000_000); 2], 10_000).unwrap();
        let large = CaptureBudget {
            stall_ms: u32::MAX,
            burst_records: u32::MAX,
            ..BUDGET
        };
        assert!(large.validate([Some(u32::MAX); 2], u32::MAX).is_err());
    }
}
