use std::io;
use std::sync::mpsc::{RecvTimeoutError, TryRecvError, TrySendError};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use crate::control::Notification;
use gnss::GnssFix;
use imu::ImuSample;
use messaging::{bounded, Inbox, Sender};

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct NavState {
    pub timestamp_ms: u64,
    pub north_m: f32,
    pub east_m: f32,
    pub speed_mps: f32,
    pub heading_rad: f32,
    pub imu_sequence: u64,
    /// Missing/reordered input intervals; integration skips these discontinuities.
    pub imu_discontinuities: u64,
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
    sender: Sender<Event>,
}

#[derive(Clone)]
pub struct GnssInput {
    sender: Sender<Event>,
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

fn submit(sender: &Sender<Event>, event: Event) -> Submit {
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
    inbox: Inbox<Event>,
    control: Sender<Event>,
    output: Sender<NavState>,
    output_rx: Option<Inbox<NavState>>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct FusionStats {
    pub inputs: u64,
    pub outputs: u64,
    pub dropped_outputs: u64,
}

pub struct FusionHandle {
    control: Sender<Event>,
    output: Inbox<NavState>,
    join: Option<JoinHandle<FusionStats>>,
    stopping: Arc<AtomicBool>,
}

impl FusionService {
    pub fn new() -> (Self, FusionInputs) {
        Self::with_config(FusionConfig::default())
    }

    pub fn with_config(config: FusionConfig) -> (Self, FusionInputs) {
        assert!(
            config.inbox_capacity > 0,
            "fusion inbox capacity must be nonzero"
        );
        assert!(
            config.output_capacity > 0,
            "fusion output capacity must be nonzero"
        );
        assert!(
            config.publish_every_imu > 0,
            "fusion publish cadence must be nonzero"
        );

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

    pub fn start(self) -> io::Result<FusionHandle> {
        self.start_with_wake(|| {})
    }

    /// Signal the caller's existing wait after output admission and retirement.
    ///
    /// `wake` runs on this worker and must only provide bounded notification;
    /// it must not run the caller's handler or wait for notification capacity.
    /// Output stays in the existing bounded queue until `try_recv` consumes it.
    /// After a bounded drain, the caller must keep dispatching while data remains
    /// instead of sleeping just because several notifications coalesced.
    pub fn start_with_wake(
        mut self,
        wake: impl FnMut() + Send + 'static,
    ) -> io::Result<FusionHandle> {
        let output = self
            .output_rx
            .take()
            .expect("fusion output receiver already taken");
        let control = self.control.clone();
        let stopping = Arc::new(AtomicBool::new(false));
        let stop = stopping.clone();
        let join = thread::Builder::new()
            .name("fusion-service".into())
            .spawn(move || {
                // The signal outlives run's service/output sender even on unwind.
                let mut signal = OutputWake(Notification::new(wake));
                run(self, &stop, &mut signal.0)
            })?;
        Ok(FusionHandle {
            control,
            output,
            join: Some(join),
            stopping,
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

    pub fn stop(mut self) -> io::Result<FusionStats> {
        // Explicit teardown drains inputs admitted before this marker. Only
        // abandoned handles use immediate, nonblocking cancellation in Drop.
        let _ = self.control.send(Event::Shutdown);
        self.join
            .take()
            .expect("owned join")
            .join()
            .map_err(|_| io::Error::other("fusion service panicked"))
    }
    fn request_stop(&self) {
        self.stopping.store(true, Ordering::Release);
        let _ = self.control.try_send(Event::Shutdown);
    }
}
impl Drop for FusionHandle {
    fn drop(&mut self) {
        self.request_stop();
    }
}

struct OutputWake<W: FnMut()>(Notification<W>);

impl<W: FnMut()> Drop for OutputWake<W> {
    fn drop(&mut self) {
        self.0.retire();
    }
}

fn run(
    service: FusionService,
    stopping: &AtomicBool,
    wake: &mut Notification<impl FnMut()>,
) -> FusionStats {
    let mut core = FusionCore::default();
    let mut stats = FusionStats::default();
    let mut imu_since_output = 0u8;

    // The owner has one wait point. IMU, GNSS and lifecycle producers all fan
    // into this one bounded inbox. Each event is handled to completion before
    // the next receive.
    while !stopping.load(Ordering::Acquire) {
        let Ok(event) = service.inbox.wait(None) else {
            break;
        };
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
            Ok(()) => {
                stats.outputs = stats.outputs.saturating_add(1);
                wake.notify();
            }
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
    last_imu: Option<(u64, Option<u64>)>,
    origin: Option<(f64, f64)>,
}

impl FusionCore {
    fn on_imu(&mut self, sample: ImuSample) -> NavState {
        let capture_us = sample.is_capture_time.then_some(sample.timestamp_us);
        if let Some((last, last_us)) = self.last_imu {
            let elapsed_us = match (last_us, capture_us) {
                (Some(last), Some(now)) if now > last => Some(now - last),
                (None, None) => Some(u64::from(sample.sample_interval_us)),
                _ => None,
            };
            if let Some(elapsed_us) =
                elapsed_us.filter(|_| Some(sample.sequence) == last.checked_add(1))
            {
                let dt = elapsed_us as f32 / 1_000_000.0;
                self.state.heading_rad += sample.gyro_rps[2] * dt;
                self.state.speed_mps = (self.state.speed_mps + sample.accel_mps2[0] * dt).max(0.0);
            } else {
                self.state.imu_discontinuities = self.state.imu_discontinuities.saturating_add(1);
            }
        }
        self.last_imu = Some((sample.sequence, capture_us));
        self.state.timestamp_ms = sample.timestamp_us / 1_000;
        self.state.imu_sequence = sample.sequence;
        self.state
    }

    fn on_gnss(&mut self, fix: GnssFix) -> NavState {
        let (latitude, longitude) = *self
            .origin
            .get_or_insert((fix.latitude_deg, fix.longitude_deg));
        let radius = 6_378_137.0;
        // Local tangent approximation, anchored at the first accepted fix.
        let north = (fix.latitude_deg - latitude).to_radians() * radius;
        let delta_lon = (fix.longitude_deg - longitude + 180.0).rem_euclid(360.0) - 180.0;
        let east = delta_lon.to_radians() * radius * latitude.to_radians().cos();
        self.state.timestamp_ms = fix.timestamp_us / 1_000;
        self.state.north_m = north as f32;
        self.state.east_m = east as f32;
        self.state.speed_mps = fix.speed_mps;
        if let Some(course) = fix.course_rad {
            self.state.heading_rad = course;
        }
        self.state.gnss_sequence = fix.sequence;
        self.state
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn projects_relative_to_first_fix_and_preserves_heading_when_course_is_unknown() {
        let mut core = FusionCore::default();
        let mut fix = GnssFix {
            generation: 1,
            source_epoch_ms: 0,
            sequence: 1,
            timestamp_us: 0,
            received_us: 0,
            is_capture_time: false,
            receiver_utc_ms: None,
            latitude_deg: 45.0,
            longitude_deg: 179.999,
            speed_mps: 1.0,
            course_rad: Some(0.8),
            quality: None,
        };
        let state = core.on_gnss(fix);
        assert_eq!((state.north_m, state.east_m), (0.0, 0.0));
        fix.latitude_deg += 0.001;
        fix.longitude_deg = -179.999;
        fix.course_rad = None;
        let state = core.on_gnss(fix);
        assert!((state.north_m - 111.3195).abs() < 0.001);
        assert!((state.east_m - 157.4295).abs() < 0.001);
        assert_eq!(state.heading_rad, 0.8);
    }
}

#[cfg(test)]
mod fifo_timing_tests {
    use super::*;
    #[test]
    fn consecutive_samples_use_capture_time_and_reject_a_repeated_capture() {
        let mut core = FusionCore::default();
        let mut sample = ImuSample {
            sequence: 1,
            timestamp_us: 30_000,
            received_us: 30_000,
            is_capture_time: true,
            sample_interval_us: 10_000,
            accel_mps2: [0.0; 3],
            gyro_rps: [0.0, 0.0, 1.0],
        };
        core.on_imu(sample);
        sample.sequence += 1;
        sample.timestamp_us += 12_000;
        assert!((core.on_imu(sample).heading_rad - 0.012).abs() < 1e-6);
        sample.sequence += 1;
        assert_eq!(core.on_imu(sample).imu_discontinuities, 1);
        assert!((core.state.heading_rad - 0.012).abs() < 1e-6);
    }

    #[test]
    fn batched_samples_use_cadence_but_gaps_never_extrapolate_unobserved_motion() {
        let mut core = FusionCore::default();
        let sample = ImuSample {
            sequence: 1,
            timestamp_us: 123_000,
            received_us: 123_000,
            is_capture_time: false,
            sample_interval_us: 10_000,
            accel_mps2: [0.0; 3],
            gyro_rps: [0.0, 0.0, 1.0],
        };
        core.on_imu(sample);
        core.on_imu(ImuSample {
            sequence: 2,
            ..sample
        });
        let state = core.on_imu(ImuSample {
            sequence: 4,
            gyro_rps: [0.0, 0.0, 100.0],
            ..sample
        });
        assert!((state.heading_rad - 0.01).abs() < 1e-6);
        assert_eq!(state.imu_discontinuities, 1);
    }
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;
    #[test]
    fn a_panicking_output_wake_remains_a_joined_worker_failure() {
        let (fusion, inputs) = FusionService::with_config(FusionConfig {
            publish_every_imu: 1,
            ..FusionConfig::default()
        });
        let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let count = calls.clone();
        let handle = fusion
            .start_with_wake(move || {
                count.fetch_add(1, Ordering::SeqCst);
                panic!("output wake failed");
            })
            .unwrap();
        assert_eq!(
            inputs.imu.submit(ImuSample {
                sequence: 1,
                timestamp_us: 0,
                received_us: 0,
                is_capture_time: false,
                sample_interval_us: 10_000,
                accel_mps2: [0.0; 3],
                gyro_rps: [0.0; 3],
            }),
            Submit::Accepted
        );
        assert_eq!(
            handle.stop().unwrap_err().to_string(),
            "fusion service panicked"
        );
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn wake_follows_output_publication_and_channel_retirement() {
        let (fusion, inputs) = FusionService::with_config(FusionConfig {
            publish_every_imu: 1,
            ..FusionConfig::default()
        });
        let (wake, notifications) = std::sync::mpsc::sync_channel(1);
        let handle = fusion
            .start_with_wake(move || {
                let _ = wake.try_send(());
            })
            .unwrap();
        assert_eq!(
            inputs.imu.submit(ImuSample {
                sequence: 1,
                timestamp_us: 0,
                received_us: 0,
                is_capture_time: false,
                sample_interval_us: 10_000,
                accel_mps2: [0.0; 3],
                gyro_rps: [0.0; 3],
            }),
            Submit::Accepted
        );
        notifications.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(handle.try_recv().unwrap().imu_sequence, 1);
        handle.control.try_send(Event::Shutdown).unwrap();
        notifications.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(handle.try_recv(), Err(TryRecvError::Disconnected));
        handle.stop().unwrap();
    }

    #[test]
    fn explicit_stop_drains_previously_admitted_inputs() {
        let (fusion, inputs) = FusionService::new();
        for sequence in 1..=5 {
            assert_eq!(
                inputs.imu.submit(ImuSample {
                    sequence,
                    timestamp_us: 0,
                    received_us: 0,
                    is_capture_time: false,
                    sample_interval_us: 10_000,
                    accel_mps2: [0.0; 3],
                    gyro_rps: [0.0; 3],
                }),
                Submit::Accepted
            );
        }
        assert_eq!(fusion.start().unwrap().stop().unwrap().inputs, 5);
    }
    #[test]
    fn dropped_fusion_handle_retires_its_inbox_after_partial_app_startup() {
        let (fusion, inputs) = FusionService::new();
        let handle = fusion.start().unwrap();
        drop(handle);
        let deadline = std::time::Instant::now() + Duration::from_secs(1);
        let sample = ImuSample {
            sequence: 1,
            timestamp_us: 0,
            received_us: 0,
            is_capture_time: false,
            sample_interval_us: 10_000,
            accel_mps2: [0.0; 3],
            gyro_rps: [0.0; 3],
        };
        while !matches!(inputs.imu.submit(sample), Submit::Disconnected) {
            assert!(std::time::Instant::now() < deadline, "fusion task leaked");
            std::thread::yield_now();
        }
    }
}
