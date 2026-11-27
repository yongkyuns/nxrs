//! Product admission budget, checked before creating acquisition endpoints.
use messaging::pooled::CAPACITY;
use std::io;

/// Requested Rust heap for the pool, including its shared control allocation.
/// Allocator bookkeeping, the wake/clock view, transport buffers and task stacks
/// are accounted separately. This arena stays reserved while export is idle.
pub(super) const POOL_REQUESTED_BYTES: usize = 22 * 1024;
const STALL_MS: u32 = 2_000;
const BURST_RECORDS: u32 = 64;
// Current product profiles configure a 500 ms navigation period. The portable
// GNSS Device API does not expose that configuration, so this is an explicit
// product assumption, not a discovered or enforced hardware rate. Faster GNSS
// requires revisiting this budget; actual excess traffic closes on overflow.
const GNSS_RATE_MILLIHZ: u32 = 2_000;

const fn records_during_stall(rate_millihz: u32) -> u64 {
    (rate_millihz as u64 * STALL_MS as u64).div_ceil(1_000_000)
}

const fn required_slots(rates: [u32; 2]) -> u64 {
    records_during_stall(rates[0])
        + records_during_stall(rates[1])
        + records_during_stall(GNSS_RATE_MILLIHZ)
        + BURST_RECORDS as u64
}

// Keep the documented nominal workload viable when capacity or policy changes.
const _: () = assert!(required_slots([100_000, 100_000]) <= CAPACITY as u64);

pub(super) fn validate_imu_rates(rates: [Option<u32>; 2]) -> io::Result<()> {
    // Failed/unsupported sources have no admitted traffic. Their original
    // acquisition errors remain the runtime's responsibility.
    if rates.contains(&Some(0)) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "capture requires a nonzero resolved IMU rate",
        ));
    }
    let required = required_slots(rates.map(|rate| rate.unwrap_or(0)));
    if required > CAPACITY as u64 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "capture needs {required} slots for a {STALL_MS} ms stall and \
                 {BURST_RECORDS} burst records; capacity is {CAPACITY}"
            ),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn supported_rates_fit_the_declared_stall_and_burst() {
        assert_eq!(required_slots([100_000, 100_000]), 468);
        assert_eq!(required_slots([104_000, 104_000]), 484);
        validate_imu_rates([Some(104_000), Some(104_000)]).unwrap();
        validate_imu_rates([Some(200_000), None]).unwrap();
    }

    #[test]
    fn rate_budget_rejects_overcommit_instead_of_reducing_stall_cover() {
        validate_imu_rates([Some(111_000), Some(111_000)]).unwrap();
        assert_eq!(required_slots([111_000, 111_000]), CAPACITY as u64);
        for rates in [
            [Some(111_001), Some(111_000)],
            [Some(u32::MAX), Some(u32::MAX)],
            [Some(0), None],
        ] {
            assert_eq!(
                validate_imu_rates(rates).unwrap_err().kind(),
                io::ErrorKind::InvalidInput
            );
        }
    }
}
