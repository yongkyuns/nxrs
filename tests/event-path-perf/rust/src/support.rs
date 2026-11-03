use crate::native::{self, Gate, Result, Schedule, State};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, OnceLock,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

pub const WAIT_MS: u32 = 1500;
pub const WORKER_STACK: usize = 16 * 1024;
pub const SENDER_PRIORITY: i32 = 120;
pub const RECEIVER_PRIORITY: i32 = 180;
pub const OBSERVER_PRIORITY: i32 = 220;

#[derive(Clone, Copy, Debug)]
pub struct Config {
    pub samples: u32,
    pub warmup: u32,
}
impl Config {
    pub fn new(samples: u32, warmup: u32) -> Result<Self> {
        if !(1..=4096).contains(&samples) || warmup > 1024 {
            return Err("count-out-of-bounds");
        }
        Ok(Self { samples, warmup })
    }
    pub fn total(self) -> u32 {
        self.samples + self.warmup
    }
}

pub fn configure(priority: i32) -> Result<Schedule> {
    if native::is_nuttx() {
        native::fifo(native::tid(), priority)
    } else {
        native::schedule(native::tid())
    }
}

pub fn wait_until(mut predicate: impl FnMut() -> bool) -> Result<()> {
    let deadline = Instant::now() + Duration::from_millis(WAIT_MS.into());
    while !predicate() {
        if Instant::now() >= deadline {
            return Err("observation-deadline");
        }
        thread::sleep(Duration::from_millis(1));
    }
    Ok(())
}

pub fn blocked(tid: i32, schedule: Schedule, mut current: impl FnMut() -> bool) -> Result<bool> {
    if !native::is_nuttx() {
        return Ok(false);
    }
    wait_until(|| {
        let (state, priority) = native::state(tid);
        state == State::WaitSem && priority == schedule.priority && current()
    })?;
    Ok(true)
}

pub fn join_bounded<T>(handle: JoinHandle<Result<T>>) -> Result<T> {
    if wait_until(|| handle.is_finished()).is_err() {
        crate::fatal("worker-join-deadline");
    }
    handle.join().map_err(|_| "worker-panic")?
}

pub struct Clock {
    pub hz: u64,
    pub source: u32,
    pub bits: u32,
    pub resolution_ns: u64,
}
impl Clock {
    pub fn new() -> Result<Self> {
        // Native counter selection is setup-only global state. Initialize once,
        // including in parallel host unit tests, before any counter readers.
        static INFO: OnceLock<Result<(u64, u32, u32, u64)>> = OnceLock::new();
        let (hz, source, bits, resolution_ns) = *INFO
            .get_or_init(|| {
                let request = u32::from(env!("NXRS_STD_CHANNEL_CLOCK_SOURCE") == "native-counter");
                let (hz, source, bits) = native::counter_info(request)?;
                if hz == 0 {
                    return Err("counter-frequency");
                }
                Ok((hz, source, bits, native::resolution_ns()?))
            })
            .as_ref()
            .map_err(|error| *error)?;
        Ok(Self {
            hz,
            source,
            bits,
            resolution_ns,
        })
    }
    pub fn window(&self, begin_ns: u64, end_ns: u64, deltas: &[u32]) -> Result<u64> {
        let upper = end_ns
            .checked_sub(begin_ns)
            .and_then(|v| v.checked_add(self.resolution_ns.saturating_mul(2)))
            .ok_or("monotonic-window")?;
        // Guard against both full-wrap ambiguity and the half-range ordering
        // convention. This is a consistency guard, never clock calibration.
        if u128::from(upper) * u128::from(self.hz) >= (1u128 << 31) * 1_000_000_000
            || deltas.iter().any(|&v| v >= 1 << 31)
        {
            return Err("counter-wrap-ambiguous");
        }
        if deltas
            .iter()
            .any(|&v| u128::from(v) * 1_000_000_000 > u128::from(upper) * u128::from(self.hz))
        {
            return Err("counter-outside-monotonic-window");
        }
        Ok(upper)
    }
}

pub struct Watchdog {
    stop: Arc<Gate>,
    handle: JoinHandle<Result<()>>,
}
impl Watchdog {
    pub fn start() -> Result<Self> {
        Self::start_with(|| configure(240))
    }
    fn start_with(
        configure_watchdog: impl FnOnce() -> Result<Schedule> + Send + 'static,
    ) -> Result<Self> {
        let stop = Gate::new()?;
        let ready = Gate::new()?;
        let configured = Arc::new(AtomicBool::new(false));
        let worker_configured = configured.clone();
        let (worker_stop, worker_ready) = (stop.clone(), ready.clone());
        let handle = thread::Builder::new()
            .stack_size(8192)
            .spawn(move || {
                let configuration = configure_watchdog();
                worker_configured.store(configuration.is_ok(), Ordering::Release);
                worker_ready.post()?;
                configuration?;
                if worker_stop.wait(30000).is_err() {
                    crate::fatal("watchdog-deadline");
                }
                Ok(())
            })
            .map_err(|_| "watchdog-spawn")?;
        if ready.wait(WAIT_MS).is_err() {
            crate::fatal("watchdog-setup");
        }
        if !configured.load(Ordering::Acquire) {
            join_bounded(handle)?;
            return Err("watchdog-scheduling");
        }
        if handle.is_finished() {
            join_bounded(handle)?;
            return Err("watchdog-not-running");
        }
        Ok(Self { stop, handle })
    }
    pub fn finish(self) -> Result<()> {
        if self.stop.post().is_err() {
            crate::fatal("watchdog-stop");
        }
        join_bounded(self.handle)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn counts_and_wrap_guard_refuse_ambiguous_intervals() {
        assert!(Config::new(0, 0).is_err());
        assert!(Config::new(4097, 0).is_err());
        assert!(Config::new(1, 1025).is_err());
        let clock = Clock {
            hz: 1_000_000_000,
            source: 7,
            bits: 64,
            resolution_ns: 1,
        };
        assert!(clock.window(10, 9, &[1]).is_err());
        assert!(clock.window(0, 1u64 << 31, &[1]).is_err());
        assert!(clock.window(0, 10, &[1 << 31]).is_err());
        assert!(clock.window(0, 10, &[13]).is_err());
        assert_eq!(clock.window(0, 10, &[10]).unwrap(), 12);
    }
    #[test]
    fn watchdog_scheduling_failure_is_not_an_active_watchdog() {
        assert!(matches!(
            Watchdog::start_with(|| Err("injected-scheduling-failure")),
            Err("injected-scheduling-failure")
        ));
    }
}
