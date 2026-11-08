//! Pure request validation and nominal rate selection.
use super::Error;

/// Requirements for one paired accelerometer/gyroscope stream.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    /// Inclusive nominal rate limits in millihertz.
    pub min_rate_millihz: u32,
    pub max_rate_millihz: u32,
    /// Exact accelerometer full scale in multiples of standard gravity.
    pub accel_range_g: u16,
    /// Exact gyroscope full scale in degrees per second.
    pub gyro_range_dps: u16,
    /// Maximum nominal time between delivery opportunities. Scheduling,
    /// bus transfer time and sink backpressure contribute additional latency.
    pub max_delivery_interval_us: u32,
}

/// Nominal settings accepted by a driver; hardware construction verifies them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AppliedConfig {
    pub rate_millihz: u32,
    /// Exact accelerometer full scale in multiples of standard gravity.
    pub accel_range_g: u16,
    /// Exact gyroscope full scale in degrees per second.
    pub gyro_range_dps: u16,
    /// Nominal interval between delivery opportunities in microseconds.
    pub delivery_interval_us: u32,
}

/// Select the slowest driver rate satisfying the requested sampling and delivery bounds.
pub fn select_rate<R: Copy>(
    config: Config,
    rates: impl IntoIterator<Item = R>,
    milli_hz: impl Fn(R) -> u32,
) -> Result<(R, AppliedConfig), Error> {
    if config.min_rate_millihz == 0
        || config.min_rate_millihz > config.max_rate_millihz
        || config.max_delivery_interval_us == 0
        || config.accel_range_g == 0
        || config.gyro_range_dps == 0
    {
        return Err(Error::InvalidData);
    }
    let minimum = config
        .min_rate_millihz
        .max(1_000_000_000u32.div_ceil(config.max_delivery_interval_us));
    rates
        .into_iter()
        .filter(|rate| (minimum..=config.max_rate_millihz).contains(&milli_hz(*rate)))
        .min_by_key(|rate| milli_hz(*rate))
        .map(|rate| {
            let rate_millihz = milli_hz(rate);
            (
                rate,
                AppliedConfig {
                    rate_millihz,
                    accel_range_g: config.accel_range_g,
                    gyro_range_dps: config.gyro_range_dps,
                    delivery_interval_us: 1_000_000_000u32.div_ceil(rate_millihz),
                },
            )
        })
        .ok_or(Error::Unsupported)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selection_uses_all_bounds_and_rounds_delivery_up() {
        let mut request = Config {
            min_rate_millihz: 26_000,
            max_rate_millihz: 104_000,
            accel_range_g: 4,
            gyro_range_dps: 2000,
            max_delivery_interval_us: 9_616,
        };
        let rates = [208_000, 104_000, 52_000, 26_000];
        let (rate, applied) = select_rate(request, rates, core::convert::identity).unwrap();
        assert_eq!((rate, applied.delivery_interval_us), (104_000, 9_616));
        request.max_delivery_interval_us = 9_615;
        assert_eq!(
            select_rate(request, rates, core::convert::identity),
            Err(Error::Unsupported)
        );
        request.max_delivery_interval_us = u32::MAX;
        assert_eq!(
            select_rate(request, rates, core::convert::identity)
                .unwrap()
                .0,
            26_000
        );
        assert_eq!(
            select_rate(request, [], core::convert::identity),
            Err(Error::Unsupported)
        );
    }
}
