use std::f32::consts::PI;
use std::io;
use std::sync::mpsc::{sync_channel, RecvTimeoutError, SyncSender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use gnss::{DeviceError, Gnss, GnssFix};
use event::{bounded, EventInbox, EventSender};

use crate::fusion::{GnssInput, Submit};

const INBOX_CAPACITY: usize = 4;

#[derive(Clone, Copy, Debug)]
pub struct GnssConfig {
    pub period: Duration,
}

impl Default for GnssConfig {
    fn default() -> Self {
        Self {
            period: Duration::from_millis(200),
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct GnssProcessor {
    course_offset_rad: f32,
}

impl Default for GnssProcessor {
    fn default() -> Self {
        Self {
            course_offset_rad: 0.0,
        }
    }
}

impl GnssProcessor {
    pub fn with_course_offset(course_offset_rad: f32) -> Self {
        Self { course_offset_rad }
    }

    pub fn process(&mut self, mut fix: GnssFix) -> GnssFix {
        fix.speed_mps = fix.speed_mps.max(0.0);
        fix.course_rad = wrap_pi(fix.course_rad + self.course_offset_rad);
        fix
    }
}

fn wrap_pi(mut value: f32) -> f32 {
    while value > PI {
        value -= 2.0 * PI;
    }
    while value < -PI {
        value += 2.0 * PI;
    }
    value
}

pub struct GnssService {
    config: GnssConfig,
    processor: GnssProcessor,
    output: GnssInput,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct GnssStats {
    pub produced: u64,
    pub dropped: u64,
    pub errors: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct GnssStatus {
    pub sampling: bool,
    pub period: Duration,
    pub stats: GnssStats,
}

enum Event {
    SetPeriod(Duration, SyncSender<()>),
    Pause(SyncSender<()>),
    Resume(SyncSender<()>),
    Status(SyncSender<GnssStatus>),
    FixDeadline,
    Shutdown,
}

pub struct GnssHandle {
    control: EventSender<Event>,
    join: JoinHandle<GnssStats>,
}

impl GnssService {
    pub fn new(output: GnssInput) -> Self {
        Self {
            config: GnssConfig::default(),
            processor: GnssProcessor::default(),
            output,
        }
    }

    pub fn with_config(mut self, config: GnssConfig) -> Self {
        assert!(!config.period.is_zero(), "GNSS period must be nonzero");
        self.config = config;
        self
    }

    pub fn with_processor(mut self, processor: GnssProcessor) -> Self {
        self.processor = processor;
        self
    }

    pub fn start(self) -> io::Result<GnssHandle> {
        let device = gnss::open().map_err(|error| hal_error("GNSS", error))?;
        let (control, inbox) = bounded(INBOX_CAPACITY);
        let join = thread::Builder::new()
            .name("gnss-service".into())
            .spawn(move || run(self, device, inbox))?;
        Ok(GnssHandle { control, join })
    }
}

impl GnssHandle {
    pub fn set_period(&self, period: Duration) -> io::Result<()> {
        if period.is_zero() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "GNSS period must be nonzero",
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

    pub fn status(&self) -> io::Result<GnssStatus> {
        let (reply, response) = sync_channel(1);
        self.send(Event::Status(reply))?;
        response
            .recv()
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "GNSS status reply closed"))
    }

    pub fn stop(self) -> io::Result<GnssStats> {
        let _ = self.control.send(Event::Shutdown);
        self.join
            .join()
            .map_err(|_| io::Error::other("GNSS service panicked"))
    }

    fn request(&self, event: impl FnOnce(SyncSender<()>) -> Event) -> io::Result<()> {
        let (reply, response) = sync_channel(1);
        self.send(event(reply))?;
        response
            .recv()
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "GNSS command reply closed"))
    }

    fn send(&self, event: Event) -> io::Result<()> {
        self.control
            .send(event)
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "GNSS service stopped"))
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

fn run<D>(mut service: GnssService, mut device: D, inbox: EventInbox<Event>) -> GnssStats
where
    D: Gnss,
{
    let origin = Instant::now();
    let mut stats = GnssStats::default();
    let mut period = service.config.period;
    let mut sampling = true;
    let mut next_fix = Instant::now() + period;

    loop {
        // One logical wait point. Commands and deadline expiry are serialized
        // through one event-dispatch path.
        let wait = sampling.then(|| next_fix.saturating_duration_since(Instant::now()));
        let event = match inbox.wait(wait) {
            Ok(event) => event,
            Err(RecvTimeoutError::Timeout) => Event::FixDeadline,
            Err(RecvTimeoutError::Disconnected) => break,
        };

        // Run this event to completion before returning to the wait point.
        match event {
            Event::SetPeriod(value, reply) => {
                period = value;
                if sampling {
                    next_fix = Instant::now() + period;
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
                    next_fix = Instant::now() + period;
                }
                let _ = reply.send(());
            }
            Event::Status(reply) => {
                let _ = reply.send(GnssStatus {
                    sampling,
                    period,
                    stats,
                });
            }
            Event::FixDeadline => {
                let now_ms = u64::try_from(origin.elapsed().as_millis()).unwrap_or(u64::MAX);
                match device.fix(now_ms) {
                    Ok(fix) => {
                        let fix = service.processor.process(fix);
                        stats.produced = stats.produced.saturating_add(1);
                        match service.output.submit(fix) {
                            Submit::Accepted => {}
                            Submit::Dropped => {
                                stats.dropped = stats.dropped.saturating_add(1);
                            }
                            Submit::Disconnected => break,
                        }
                    }
                    Err(_) => stats.errors = stats.errors.saturating_add(1),
                }

                let now = Instant::now();
                next_fix += period;
                if next_fix <= now {
                    next_fix = now + period;
                }
            }
            Event::Shutdown => break,
        }
    }
    stats
}
