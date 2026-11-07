//! Fixed-size, task-context timing summaries printed after acquisition joins.
use imu::probe::Timing;
use std::{
    sync::{
        atomic::{AtomicU32, Ordering},
        Mutex,
    },
    time::{Duration, Instant},
};

const BOUNDS_US: [u64; 10] = [10, 25, 50, 100, 250, 500, 1_000, 2_500, 10_000, 50_000];

#[derive(Default)]
struct Histogram {
    buckets: [u64; BOUNDS_US.len() + 1],
    max_us: u64,
}

impl Histogram {
    fn record(&mut self, duration: Duration) {
        let us = u64::try_from(duration.as_nanos().div_ceil(1_000)).unwrap_or(u64::MAX);
        self.buckets[BOUNDS_US.partition_point(|&bound| bound < us)] += 1;
        self.max_us = self.max_us.max(us);
    }

    fn write_json(&self, name: &str) {
        print!(
            ",\"{name}\":{{\"max_us\":{},\"buckets\":{:?}}}",
            self.max_us, self.buckets
        );
    }
}

struct Wake {
    at: Instant,
    first_ioctl: bool,
}

#[derive(Default)]
struct Observations {
    active: Option<Wake>,
    completed: u64,
    incomplete: u64,
    missing_ioctl: u64,
    unmatched_drain: u64,
    periodic_lateness: Histogram,
    wake_to_ioctl: Histogram,
    ioctl: Histogram,
    drain: Histogram,
}

impl Observations {
    fn record(&mut self, event: Timing) {
        match event {
            Timing::Wake {
                at,
                periodic_deadline,
            } => {
                if self
                    .active
                    .replace(Wake {
                        at,
                        first_ioctl: false,
                    })
                    .is_some()
                {
                    self.incomplete += 1;
                }
                if let Some(deadline) = periodic_deadline {
                    self.periodic_lateness.record(at.duration_since(deadline));
                }
            }
            Timing::Ioctl { start, end } => {
                // Sensor setup and stop also call the bus. Only record the
                // ioctl intervals that belong to acquisition drains.
                if let Some(wake) = &mut self.active {
                    if !wake.first_ioctl {
                        self.wake_to_ioctl.record(start.duration_since(wake.at));
                        wake.first_ioctl = true;
                    }
                    self.ioctl.record(end.duration_since(start));
                }
            }
            Timing::Drain { at, complete } => {
                let Some(wake) = self.active.take() else {
                    self.unmatched_drain += 1;
                    return;
                };
                if complete {
                    self.completed += 1;
                    if !wake.first_ioctl {
                        self.missing_ioctl += 1;
                    }
                    self.drain.record(at.duration_since(wake.at));
                } else {
                    self.incomplete += 1;
                }
            }
        }
    }
}

#[derive(Default)]
pub(crate) struct Recorder {
    observations: Mutex<Observations>,
    dropped: AtomicU32,
}

impl Recorder {
    pub(crate) fn record(&self, event: Timing) {
        if let Ok(mut observations) = self.observations.try_lock() {
            observations.record(event);
        } else {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    pub(crate) fn report(&self) -> bool {
        let seen = self.observations.lock().expect("observer lock after join");
        let dropped = self.dropped.load(Ordering::Relaxed);
        let incomplete = seen.incomplete + u64::from(seen.active.is_some());
        let valid = seen.completed != 0
            && incomplete == 0
            && seen.missing_ioctl == 0
            && seen.unmatched_drain == 0
            && dropped == 0;
        print!(
            "{{\"timing_schema\":1,\"valid\":{valid},\"bounds_us\":{BOUNDS_US:?},\
             \"completed\":{},\"incomplete\":{incomplete},\"missing_ioctl\":{},\
             \"unmatched_drain\":{},\"dropped\":{dropped}",
            seen.completed, seen.missing_ioctl, seen.unmatched_drain,
        );
        seen.periodic_lateness.write_json("periodic_lateness");
        seen.wake_to_ioctl.write_json("wake_to_first_ioctl");
        seen.ioctl.write_json("ioctl");
        seen.drain.write_json("drain");
        println!("}}");
        valid
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn times_only_active_drains_and_counts_incomplete_wakes() {
        let now = Instant::now();
        let at = |us| now + Duration::from_micros(us);
        let mut seen = Observations::default();
        seen.record(Timing::Ioctl {
            start: at(0),
            end: at(15),
        });
        seen.record(Timing::Wake {
            at: at(100),
            periodic_deadline: Some(at(80)),
        });
        seen.record(Timing::Ioctl {
            start: at(110),
            end: at(135),
        });
        seen.record(Timing::Ioctl {
            start: at(140),
            end: at(200),
        });
        seen.record(Timing::Drain {
            at: at(220),
            complete: true,
        });
        seen.record(Timing::Wake {
            at: at(250),
            periodic_deadline: None,
        });
        seen.record(Timing::Drain {
            at: at(275),
            complete: false,
        });
        seen.record(Timing::Ioctl {
            start: at(280),
            end: at(300),
        });
        assert_eq!(seen.completed, 1);
        assert_eq!(seen.incomplete, 1);
        assert_eq!(seen.periodic_lateness.max_us, 20);
        assert_eq!(seen.wake_to_ioctl.max_us, 10);
        assert_eq!(seen.ioctl.max_us, 60);
        assert_eq!(seen.ioctl.buckets[..4], [0, 1, 0, 1]);
        assert_eq!(seen.drain.max_us, 120);
    }
}
