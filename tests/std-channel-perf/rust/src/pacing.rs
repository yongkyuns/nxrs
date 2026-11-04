//! Deterministic, bounded setup pacing. Never a production wait or channel path.
use crate::{native, Clock, Result, WAIT_MS};

#[derive(Clone, Copy, Default)]
pub struct Observation {
    pub requested_ticks: u32,
    pub start_ticks: u32,
    pub return_ticks: u32,
    pub elapsed_upper_ns: u64,
}

#[derive(Clone, Copy)]
pub enum Schedule {
    Tight,
    Seeded(u32),
    Fixed(u32),
}

impl Schedule {
    pub fn parse(value: Option<&str>) -> Result<Self> {
        match value {
            None => Ok(Self::Tight),
            Some(value) => {
                if let Some(delay) = value.strip_prefix("fixed:") {
                    let delay = delay.parse::<u32>().map_err(|_| "invalid-fixed-delay")?;
                    if !(1..=2000).contains(&delay) {
                        return Err("fixed-delay-out-of-range");
                    }
                    Ok(Self::Fixed(delay))
                } else {
                    let seed = value.parse::<u32>().map_err(|_| "invalid-phase-seed")?;
                    if seed == 0 {
                        return Err("zero-phase-seed");
                    }
                    Ok(Self::Seeded(seed))
                }
            }
        }
    }
}

pub struct Pacing {
    schedule: Schedule,
    seed: u32,
    state: u32,
}

impl Pacing {
    pub fn new(schedule: Schedule, samples: u32, warmup: u32) -> Result<Self> {
        match schedule {
            Schedule::Seeded(0) => return Err("zero-phase-seed"),
            Schedule::Fixed(value) if !(1..=2000).contains(&value) => {
                return Err("fixed-delay-out-of-range");
            }
            _ => {}
        }
        let seed = match schedule {
            Schedule::Seeded(seed) => seed,
            _ => 0,
        };
        // At most 2.304 seconds of requested active delay. Leave ample room
        // for handshakes and cleanup under the unchanged 15-second watchdog.
        if !matches!(schedule, Schedule::Tight) && (samples > 1024 || warmup > 128) {
            return Err("phase-count-budget");
        }
        Ok(Self {
            schedule,
            seed,
            state: seed,
        })
    }

    pub fn seed(&self) -> u32 {
        self.seed
    }

    pub fn profile(&self) -> &'static str {
        match self.schedule {
            Schedule::Tight => "tight-v1",
            Schedule::Seeded(_) => "setup-counter-delay-v1",
            Schedule::Fixed(_) => "setup-fixed-counter-delay-v1",
        }
    }

    pub fn fixed_delay_us(&self) -> u32 {
        match self.schedule {
            Schedule::Fixed(value) => value,
            _ => 0,
        }
    }

    fn next_us(&mut self) -> u32 {
        match self.schedule {
            Schedule::Tight => return 0,
            Schedule::Fixed(value) => return value,
            Schedule::Seeded(_) => {}
        }
        // Versioned xorshift32: analysis replays every warmup and sample.
        self.state ^= self.state << 13;
        self.state ^= self.state >> 17;
        self.state ^= self.state << 5;
        self.state % 2000 + 1
    }

    pub fn wait(&mut self, clock: &Clock) -> Result<Observation> {
        let delay_us = self.next_us();
        if delay_us == 0 {
            return Ok(Observation::default());
        }
        if !clock.counter_validated {
            return Err("phase-requires-validated-counter");
        }
        let ticks = (u128::from(clock.hz) * u128::from(delay_us)).div_ceil(1_000_000);
        if !(1..(1 << 31)).contains(&ticks) {
            return Err("phase-counter-range");
        }
        wait_counter(
            ticks as u32,
            clock.monotonic_resolution_ns,
            native::counter,
            native::monotonic_ns,
        )
    }
}

fn wait_counter(
    requested_ticks: u32,
    resolution_ns: u64,
    mut counter: impl FnMut() -> u32,
    mut wall: impl FnMut() -> Result<u64>,
) -> Result<Observation> {
    let before = wall()?;
    let start_ticks = counter();
    // Both the wall deadline and an iteration cap protect a broken timer.
    // Active pacing deliberately avoids rounding every delay to a kernel tick.
    // It is outside measured handoffs and provides no idle/power evidence.
    for iteration in 0..2_000_000 {
        let stamp = counter();
        let elapsed = stamp.wrapping_sub(start_ticks);
        if elapsed >= (1 << 31) {
            return Err("phase-counter-reversed");
        }
        if elapsed >= requested_ticks {
            let wall_elapsed = wall()?.checked_sub(before).ok_or("monotonic-reversed")?;
            if wall_elapsed >= u64::from(WAIT_MS) * 1_000_000 {
                return Err("phase-deadline");
            }
            let elapsed_upper_ns = wall_elapsed
                .checked_add(resolution_ns.checked_mul(2).ok_or("interval-overflow")?)
                .ok_or("interval-overflow")?;
            return Ok(Observation {
                requested_ticks,
                start_ticks,
                return_ticks: stamp,
                elapsed_upper_ns,
            });
        }
        if iteration % 256 == 0
            && wall()?.checked_sub(before).ok_or("monotonic-reversed")?
                >= u64::from(WAIT_MS) * 1_000_000
        {
            return Err("phase-deadline");
        }
        std::hint::spin_loop();
    }
    Err("phase-counter-stalled")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tight_schedule_does_not_inject_delay() {
        let mut schedule = Pacing::new(Schedule::Tight, 4096, 1024).unwrap();
        assert_eq!(schedule.next_us(), 0);
        assert_eq!(schedule.profile(), "tight-v1");
    }

    #[test]
    fn seeded_schedule_is_reproducible_and_bounded() {
        let mut first = Pacing::new(Schedule::Seeded(1), 1000, 100).unwrap();
        let mut second = Pacing::new(Schedule::Seeded(1), 1000, 100).unwrap();
        assert_eq!(first.next_us(), 370);
        assert_eq!(second.next_us(), 370);
        for _ in 0..1099 {
            let delay = first.next_us();
            assert!((1..=2000).contains(&delay));
            assert_eq!(delay, second.next_us());
        }
    }

    #[test]
    fn zero_seed_and_excessive_paced_work_are_rejected() {
        assert!(Pacing::new(Schedule::Seeded(0), 1, 0).is_err());
        assert!(Pacing::new(Schedule::Seeded(1), 1025, 0).is_err());
        assert!(Pacing::new(Schedule::Seeded(1), 1, 129).is_err());
    }

    #[test]
    fn fixed_schedule_is_explicit_constant_and_bounded() {
        for delay in [1, 100, 1000, 2000] {
            let input = format!("fixed:{delay}");
            let schedule = Schedule::parse(Some(&input)).unwrap();
            let mut pacing = Pacing::new(schedule, 1000, 101).unwrap();
            assert_eq!(pacing.profile(), "setup-fixed-counter-delay-v1");
            assert_eq!(pacing.seed(), 0);
            assert_eq!(pacing.fixed_delay_us(), delay);
            for _ in 0..1101 {
                assert_eq!(pacing.next_us(), delay);
            }
        }
        for input in ["fixed:0", "fixed:2001", "fixed:-1", "fixed:", "0", "bad"] {
            assert!(Schedule::parse(Some(input)).is_err());
        }
        assert!(Pacing::new(Schedule::Fixed(0), 1, 0).is_err());
        assert!(Pacing::new(Schedule::Fixed(2001), 1, 0).is_err());
        assert!(Pacing::new(Schedule::Fixed(1), 1025, 0).is_err());
        assert!(Pacing::new(Schedule::Fixed(1), 1, 129).is_err());
    }

    #[test]
    fn short_wrap_is_valid_but_backwards_counter_is_not() {
        let mut values = [u32::MAX - 10, 5].into_iter();
        let observation = wait_counter(15, 1, || values.next().unwrap(), || Ok(100)).unwrap();
        assert_eq!(
            observation
                .return_ticks
                .wrapping_sub(observation.start_ticks),
            16
        );
        let mut values = [100, 99].into_iter();
        assert_eq!(
            wait_counter(15, 1, || values.next().unwrap(), || Ok(100)).err(),
            Some("phase-counter-reversed")
        );
    }

    #[test]
    fn stalled_counter_has_an_independent_deadline() {
        let mut stamps = [0, u64::from(WAIT_MS) * 1_000_000].into_iter();
        assert_eq!(
            wait_counter(10, 1, || 42, || Ok(stamps.next().unwrap())).err(),
            Some("phase-deadline")
        );
    }
}
