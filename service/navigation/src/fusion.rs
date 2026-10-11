use std::io;
use std::sync::mpsc::{RecvTimeoutError, TryRecvError, TrySendError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use gnss::GnssFix;
use imu::ImuSample;
use event::{bounded, EventInbox, EventSender};

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct NavState {
    pub timestamp_ms: u64,
    pub north_m: f32,
    pub east_m: f32,
    pub speed_mps: f32,
    pub heading_rad: f32,
    pub imu_sequence: u64,
    pub gnss_sequence: u64,
}

#[derive(Clone, Copy, Debug)]
enum Event {
    Imu(ImuSample),
    Gnss(GnssFix),
    Shutdown,
}

#[derive(Clone)]
pub struct ImuInput {
    sender: EventSender<Event>,
}

#[derive(Clone)]
pub struct GnssInput {
    sender: EventSender<Event>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Submit {
    Accepted,
    Dropped,
    Disconnected,
}

impl ImuInput {
    pub(crate) fn submit(&self, sample: ImuSample) -> Submit {
        submit(&self.sender, Event::Imu(sample))
    }
}

impl GnssInput {
    pub(crate) fn submit(&self, fix: GnssFix) -> Submit {
        submit(&self.sender, Event::Gnss(fix))
    }
}

fn submit(sender: &EventSender<Event>, event: Event) -> Submit {
    match sender.try_send(event) {
        Ok(()) => Submit::Accepted,
        Err(TrySendError::Full(_)) => Submit::Dropped,
        Err(TrySendError::Disconnected(_)) => Submit::Disconnected,
    }
}

pub struct FusionInputs {
    pub imu: ImuInput,
    pub gnss: GnssInput,
}

#[derive(Clone, Copy, Debug)]
pub struct FusionConfig {
    pub inbox_capacity: usize,
    pub output_capacity: usize,
    pub publish_every_imu: u8,
}

impl Default for FusionConfig {
    fn default() -> Self {
        Self {
            inbox_capacity: 16,
            output_capacity: 4,
            publish_every_imu: 10,
        }
    }
}

pub struct FusionService {
    config: FusionConfig,
    inbox: EventInbox<Event>,
    control: EventSender<Event>,
    output: EventSender<NavState>,
    output_rx: Option<EventInbox<NavState>>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct FusionStats {
    pub inputs: u64,
    pub outputs: u64,
    pub dropped_outputs: u64,
}

pub struct FusionHandle {
    control: EventSender<Event>,
    output: EventInbox<NavState>,
    join: JoinHandle<FusionStats>,
}

impl FusionService {
    pub fn new() -> (Self, FusionInputs) {
        Self::with_config(FusionConfig::default())
    }

    pub fn with_config(config: FusionConfig) -> (Self, FusionInputs) {
        assert!(config.inbox_capacity > 0, "fusion inbox capacity must be nonzero");
        assert!(config.output_capacity > 0, "fusion output capacity must be nonzero");
        assert!(config.publish_every_imu > 0, "fusion publish cadence must be nonzero");

        let (sender, inbox) = bounded(config.inbox_capacity);
        let (output, output_rx) = bounded(config.output_capacity);
        let inputs = FusionInputs {
            imu: ImuInput {
                sender: sender.clone(),
            },
            gnss: GnssInput {
                sender: sender.clone(),
            },
        };
        (
            Self {
                config,
                inbox,
                control: sender,
                output,
                output_rx: Some(output_rx),
            },
            inputs,
        )
    }

    pub fn start(mut self) -> io::Result<FusionHandle> {
        let output = self
            .output_rx
            .take()
            .expect("fusion output receiver already taken");
        let control = self.control.clone();
        let join = thread::Builder::new()
            .name("fusion-service".into())
            .spawn(move || run(self))?;
        Ok(FusionHandle {
            control,
            output,
            join,
        })
    }
}

impl FusionHandle {
    pub fn recv_timeout(&self, timeout: Duration) -> Result<NavState, RecvTimeoutError> {
        self.output.wait(Some(timeout))
    }

    pub fn try_recv(&self) -> Result<NavState, TryRecvError> {
        self.output.try_recv()
    }

    pub fn stop(self) -> io::Result<FusionStats> {
        let _ = self.control.send(Event::Shutdown);
        self.join
            .join()
            .map_err(|_| io::Error::other("fusion service panicked"))
    }
}

fn run(service: FusionService) -> FusionStats {
    let mut core = FusionCore::default();
    let mut stats = FusionStats::default();
    let mut imu_since_output = 0u8;

    // The owner has one wait point. IMU, GNSS and lifecycle producers all fan
    // into this one bounded inbox. Each event is handled to completion before
    // the next receive.
    while let Ok(event) = service.inbox.wait(None) {
        let state = match event {
            Event::Imu(sample) => {
                stats.inputs = stats.inputs.saturating_add(1);
                imu_since_output = imu_since_output.saturating_add(1);
                let state = core.on_imu(sample);
                if imu_since_output < service.config.publish_every_imu {
                    continue;
                }
                imu_since_output = 0;
                state
            }
            Event::Gnss(fix) => {
                stats.inputs = stats.inputs.saturating_add(1);
                core.on_gnss(fix)
            }
            Event::Shutdown => break,
        };

        match service.output.try_send(state) {
            Ok(()) => stats.outputs = stats.outputs.saturating_add(1),
            Err(TrySendError::Full(_)) => {
                stats.dropped_outputs = stats.dropped_outputs.saturating_add(1);
            }
            Err(TrySendError::Disconnected(_)) => break,
        }
    }
    stats
}

#[derive(Default)]
struct FusionCore {
    state: NavState,
    last_imu_ms: Option<u64>,
}

impl FusionCore {
    fn on_imu(&mut self, sample: ImuSample) -> NavState {
        if let Some(last) = self.last_imu_ms {
            let dt = sample.timestamp_ms.saturating_sub(last) as f32 / 1000.0;
            self.state.heading_rad += sample.gyro_rps[2] * dt;
            self.state.speed_mps =
                (self.state.speed_mps + sample.accel_mps2[0] * dt).max(0.0);
        }
        self.last_imu_ms = Some(sample.timestamp_ms);
        self.state.timestamp_ms = sample.timestamp_ms;
        self.state.imu_sequence = sample.sequence;
        self.state
    }

    fn on_gnss(&mut self, fix: GnssFix) -> NavState {
        self.state.timestamp_ms = fix.timestamp_ms;
        self.state.north_m = fix.north_m;
        self.state.east_m = fix.east_m;
        self.state.speed_mps = fix.speed_mps;
        self.state.heading_rad = fix.course_rad;
        self.state.gnss_sequence = fix.sequence;
        self.state
    }
}
