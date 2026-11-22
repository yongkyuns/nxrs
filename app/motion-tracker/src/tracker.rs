use messaging::{DeliveryError, DeliveryResult, Rejected, Sender};
use std::sync::mpsc::TrySendError;
use std::time::{Duration, Instant};

const IMU_FRESH: Duration = Duration::from_millis(500);
const FIX_FRESH: Duration = Duration::from_secs(3);

#[derive(Clone, Copy)]
pub enum Input {
    Imu(usize, imu::Event),
    Gnss(gnss::Event),
}

pub struct Observation {
    input: Input,
    received: Instant,
}

pub fn sink<T: Copy, E>(
    sender: Sender<E>,
    input: impl Fn(T) -> Input,
    wrap: impl Fn(Observation) -> E,
) -> impl FnMut(T) -> DeliveryResult<T> {
    move |value| {
        let observation = Observation {
            input: input(value),
            received: Instant::now(),
        };
        sender
            .try_send(wrap(observation))
            .map_err(|error| Rejected {
                value,
                error: match error {
                    TrySendError::Full(_) => DeliveryError::Full,
                    TrySendError::Disconnected(_) => DeliveryError::Closed,
                },
            })
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Reading<T> {
    Waiting,
    Live(T),
    Stale,
    Fault,
}

impl<T> Reading<T> {
    pub fn status(&self) -> &'static str {
        match self {
            Self::Waiting => "waiting",
            Self::Live(_) => "live",
            Self::Stale => "stale",
            Self::Fault => "fault",
        }
    }
}

struct Source<T> {
    latest: Option<(T, Instant)>,
    failed: bool,
}

impl<T: Copy> Source<T> {
    fn reading(&self, now: Instant, freshness: Duration) -> Reading<T> {
        if self.failed {
            return Reading::Fault;
        }
        match self.latest {
            Some((sample, received)) if now.saturating_duration_since(received) < freshness => {
                Reading::Live(sample)
            }
            Some(_) => Reading::Stale,
            None => Reading::Waiting,
        }
    }
}

pub struct Tracker {
    imu: [Source<imu::ImuSample>; 2],
    gnss: Source<gnss::GnssFix>,
}

pub struct Snapshot {
    pub imu: [Reading<imu::ImuSample>; 2],
    pub gnss: Reading<gnss::GnssFix>,
}

impl Tracker {
    pub fn new() -> Self {
        Self {
            imu: [Source::empty(), Source::empty()],
            gnss: Source::empty(),
        }
    }

    pub fn observe(&mut self, observation: Observation) {
        let received = observation.received;
        match observation.input {
            Input::Imu(index, imu::Event::Sample(sample)) => {
                self.imu[index].latest = Some((sample, received));
            }
            Input::Imu(index, imu::Event::Failed(error)) => self.imu_failed(index, error),
            Input::Gnss(gnss::Event::Fix(fix)) => self.gnss.latest = Some((fix, received)),
            Input::Gnss(gnss::Event::InvalidFrame) => {}
            Input::Gnss(gnss::Event::Failed(error)) => self.gnss_failed(error),
        }
    }

    pub fn imu_failed(&mut self, index: usize, error: imu::DeviceError) {
        if !self.imu[index].failed {
            eprintln!("motion-tracker IMU {}: {error:?}", index + 1);
        }
        self.imu[index].failed = true;
    }

    pub fn gnss_failed(&mut self, error: gnss::DeviceError) {
        if !self.gnss.failed {
            eprintln!("motion-tracker GNSS: {error:?}");
        }
        self.gnss.failed = true;
    }

    pub fn snapshot(&self, now: Instant) -> Snapshot {
        Snapshot {
            imu: self.imu.each_ref().map(|imu| imu.reading(now, IMU_FRESH)),
            gnss: self.gnss.reading(now, FIX_FRESH),
        }
    }
}

impl<T> Source<T> {
    fn empty() -> Self {
        Self {
            latest: None,
            failed: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sensors_keep_independent_sequences_and_expire_by_reception_time() {
        let mut tracker = Tracker::new();
        let received = Instant::now();
        let sample = imu::ImuSample {
            sequence: 4,
            timestamp_ms: 0,
            sample_interval_us: 10_000,
            accel_mps2: [0.0, 0.0, 9.8],
            gyro_rps: [0.0; 3],
        };
        for index in 0..2 {
            tracker.observe(Observation {
                input: Input::Imu(index, imu::Event::Sample(sample)),
                received,
            });
        }
        assert_eq!(tracker.snapshot(received).imu, [Reading::Live(sample); 2]);
        tracker.imu_failed(0, imu::DeviceError::Io);
        assert_eq!(
            tracker.snapshot(received).imu,
            [Reading::Fault, Reading::Live(sample)]
        );
        assert_eq!(
            tracker.snapshot(received + IMU_FRESH).imu,
            [Reading::Fault, Reading::Stale],
        );
        assert_eq!(tracker.snapshot(received).gnss, Reading::Waiting);
    }
}
