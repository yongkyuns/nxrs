use std::io;
use std::sync::mpsc::{sync_channel, RecvTimeoutError, SyncSender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use imu::{DeviceError, Imu, ImuSample};
use event::{bounded, EventInbox, EventSender};

use crate::fusion::{ImuInput, Submit};

const INBOX_CAPACITY: usize = 4;

#[derive(Clone, Copy, Debug)]
pub struct ImuConfig {
    pub period: Duration,
}

impl Default for ImuConfig {
    fn default() -> Self {
        Self {
            period: Duration::from_millis(20),
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct ImuProcessor {
    accel_bias: [f32; 3],
    gyro_bias: [f32; 3],
}

impl ImuProcessor {
    pub fn with_bias(accel_bias: [f32; 3], gyro_bias: [f32; 3]) -> Self {
        Self {
            accel_bias,
            gyro_bias,
        }
    }

    pub fn process(&mut self, mut sample: ImuSample) -> ImuSample {
        for index in 0..3 {
            sample.accel_mps2[index] -= self.accel_bias[index];
            sample.gyro_rps[index] -= self.gyro_bias[index];
        }
        sample
    }
}

pub struct ImuService {
    config: ImuConfig,
    processor: ImuProcessor,
    output: ImuInput,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ImuStats {
    pub produced: u64,
    pub dropped: u64,
    pub errors: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ImuStatus {
    pub sampling: bool,
    pub period: Duration,
    pub stats: ImuStats,
}

enum Event {
    SetPeriod(Duration, SyncSender<()>),
    Pause(SyncSender<()>),
    Resume(SyncSender<()>),
    Status(SyncSender<ImuStatus>),
    SampleDeadline,
    Shutdown,
}

pub struct ImuHandle {
    control: EventSender<Event>,
    join: JoinHandle<ImuStats>,
}

impl ImuService {
    pub fn new(output: ImuInput) -> Self {
        Self {
            config: ImuConfig::default(),
            processor: ImuProcessor::default(),
            output,
        }
    }

    pub fn with_config(mut self, config: ImuConfig) -> Self {
        assert!(!config.period.is_zero(), "IMU period must be nonzero");
        self.config = config;
        self
    }

    pub fn with_processor(mut self, processor: ImuProcessor) -> Self {
        self.processor = processor;
        self
    }

    pub fn start(self) -> io::Result<ImuHandle> {
        let device = imu::open().map_err(|error| hal_error("IMU", error))?;
        let (control, inbox) = bounded(INBOX_CAPACITY);
        let join = thread::Builder::new()
            .name("imu-service".into())
            .spawn(move || run(self, device, inbox))?;
        Ok(ImuHandle { control, join })
    }
}

impl ImuHandle {
    pub fn set_period(&self, period: Duration) -> io::Result<()> {
        if period.is_zero() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "IMU period must be nonzero",
            ));
        }
        self.request(|reply| Event::SetPeriod(period, reply))
    }

    pub fn pause(&self) -> io::Result<()> {
        self.request(Event::Pause)
    }

    pub fn resume(&self) -> io::Result<()> {
        self.request(Event::Resume)
    }

    pub fn status(&self) -> io::Result<ImuStatus> {
        let (reply, response) = sync_channel(1);
        self.send(Event::Status(reply))?;
        response
            .recv()
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "IMU status reply closed"))
    }

    pub fn stop(self) -> io::Result<ImuStats> {
        let _ = self.control.send(Event::Shutdown);
        self.join
            .join()
            .map_err(|_| io::Error::other("IMU service panicked"))
    }

    fn request(&self, event: impl FnOnce(SyncSender<()>) -> Event) -> io::Result<()> {
        let (reply, response) = sync_channel(1);
        self.send(event(reply))?;
        response
            .recv()
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "IMU command reply closed"))
    }

    fn send(&self, event: Event) -> io::Result<()> {
        self.control
            .send(event)
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "IMU service stopped"))
    }
}

fn hal_error(capability: &'static str, error: DeviceError) -> io::Error {
    let kind = match error {
        DeviceError::Unsupported => io::ErrorKind::Unsupported,
        DeviceError::Busy | DeviceError::Full => io::ErrorKind::WouldBlock,
        DeviceError::Timeout => io::ErrorKind::TimedOut,
        DeviceError::Io => io::ErrorKind::Other,
        DeviceError::InvalidData => io::ErrorKind::InvalidData,
    };
    io::Error::new(kind, format!("{capability} HAL acquisition failed: {error:?}"))
}

fn run<D>(mut service: ImuService, mut device: D, inbox: EventInbox<Event>) -> ImuStats
where
    D: Imu,
{
    let origin = Instant::now();
    let mut stats = ImuStats::default();
    let mut period = service.config.period;
    let mut sampling = true;
    let mut next_sample = Instant::now() + period;

    loop {
        // One logical wait point. Commands arrive through the inbox; expiration
        // of the same recv_timeout becomes a private timer event.
        let wait = sampling.then(|| next_sample.saturating_duration_since(Instant::now()));
        let event = match inbox.wait(wait) {
            Ok(event) => event,
            Err(RecvTimeoutError::Timeout) => Event::SampleDeadline,
            Err(RecvTimeoutError::Disconnected) => break,
        };

        // Run this event to completion before returning to the wait point.
        match event {
            Event::SetPeriod(value, reply) => {
                period = value;
                if sampling {
                    next_sample = Instant::now() + period;
                }
                let _ = reply.send(());
            }
            Event::Pause(reply) => {
                sampling = false;
                let _ = reply.send(());
            }
            Event::Resume(reply) => {
                if !sampling {
                    sampling = true;
                    next_sample = Instant::now() + period;
                }
                let _ = reply.send(());
            }
            Event::Status(reply) => {
                let _ = reply.send(ImuStatus {
                    sampling,
                    period,
                    stats,
                });
            }
            Event::SampleDeadline => {
                let now_ms = u64::try_from(origin.elapsed().as_millis()).unwrap_or(u64::MAX);
                match device.sample(now_ms) {
                    Ok(sample) => {
                        let sample = service.processor.process(sample);
                        stats.produced = stats.produced.saturating_add(1);
                        match service.output.submit(sample) {
                            Submit::Accepted => {}
                            Submit::Dropped => {
                                stats.dropped = stats.dropped.saturating_add(1);
                            }
                            Submit::Disconnected => break,
                        }
                    }
                    Err(DeviceError::Busy) => {},
                    Err(_) => stats.errors = stats.errors.saturating_add(1),
                }

                let now = Instant::now();
                next_sample += period;
                if next_sample <= now {
                    next_sample = now + period;
                }
            }
            Event::Shutdown => break,
        }
    }
    stats
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Pending(bool);
    impl Imu for Pending {
        fn sample(&mut self, _: u64) -> Result<ImuSample, DeviceError> {
            let first = core::mem::replace(&mut self.0, false);
            Err(if first { DeviceError::Busy } else { DeviceError::Io })
        }
    }
    #[test]
    fn pending_data_is_normal_but_io_failure_is_counted() {
        let (_, inputs) = crate::FusionService::with_config(crate::FusionConfig::default());
        let (control, inbox) = bounded(4);
        control.send(Event::SampleDeadline).unwrap();
        control.send(Event::SampleDeadline).unwrap();
        control.send(Event::Shutdown).unwrap();
        let stats = run(ImuService::new(inputs.imu), Pending(true), inbox);
        assert_eq!((stats.produced, stats.errors), (0, 1));
    }
}
