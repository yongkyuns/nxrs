use camera::Camera;
use camera_service::{CameraService, CaptureProgress};
use recording_service::RecordingService;
use storage::Storage;
use telemetry_service::TelemetryService;
use transport::PacketSink;
use crate::{Error, Monitor, Progress, Recorder};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TickReport {
    pub camera: Result<CaptureProgress, Error>,
    pub recorder: Result<Progress, Error>,
    pub monitor: Result<Progress, Error>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ConsumerReport {
    pub recorder: Result<Progress, Error>,
    pub monitor: Result<Progress, Error>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ShutdownReport {
    pub recorder: Result<(), Error>,
    pub camera: Result<(), Error>,
}

/// Example composition, NOT a compulsory framework or service registry.
/// The owner drives capture -> recorder -> monitor; errors do not skip peers.
/// Public fields are owner-side controls. Individual apps only see their ports.
/// B chooses backing storage at construction, not application behavior.
pub struct CameraProduct<C, S, T, const BYTES: usize, const HISTORY: usize, B = [u8; BYTES]> {
    pub camera: CameraService<C, BYTES, HISTORY, B>,
    pub recordings: RecordingService<S>,
    pub telemetry: TelemetryService<T>,
    pub recorder: Recorder,
    pub monitor: Monitor,
}

impl<C: Camera, S: Storage, T: PacketSink, const BYTES: usize, const HISTORY: usize>
    CameraProduct<C, S, T, BYTES, HISTORY>
{
    /// Convenience for small inline pools. Large products should use
    /// with_buffers with platform-placed storage instead of stack temporaries.
    pub fn new(camera: C, storage: S, transport: T) -> Result<Self, Error> {
        Self::with_buffers(camera, storage, transport, core::array::from_fn(|_| [0; BYTES]), [0; BYTES])
    }
}

impl<C: Camera, S: Storage, T: PacketSink, const BYTES: usize, const HISTORY: usize, B: AsRef<[u8]> + AsMut<[u8]>>
    CameraProduct<C, S, T, BYTES, HISTORY, B>
{
    pub fn with_buffers(camera: C, storage: S, transport: T, history: [B; HISTORY], staging: B) -> Result<Self, Error> {
        Ok(Self {
            camera: CameraService::with_buffers(camera, history, staging)?,
            recordings: RecordingService::new(storage),
            telemetry: TelemetryService::new(transport),
            recorder: Recorder::default(),
            monitor: Monitor::default(),
        })
    }

    pub fn poll_camera(&mut self, now_ms: u64) -> Result<CaptureProgress, Error> {
        Ok(self.camera.poll(now_ms)?)
    }

    /// Process at most one unread frame for each local consumer without
    /// polling the camera. This lets an execution owner retry Busy sinks
    /// independently of the camera's next useful poll deadline.
    pub fn process_consumers(&mut self) -> ConsumerReport {
        let recorder = self.recorder.step(&self.camera, &mut self.recordings);
        let monitor = self.monitor.step(&self.camera, &mut self.telemetry);
        ConsumerReport { recorder, monitor }
    }

    /// Compatibility helper for deterministic/cooperative callers. Active
    /// owners should schedule poll_camera() from readiness/deadline information
    /// and use process_consumers() for independent sink retries.
    pub fn step(&mut self, now_ms: u64) -> TickReport {
        let camera = self.poll_camera(now_ms);
        let consumers = self.process_consumers();
        TickReport {
            camera,
            recorder: consumers.recorder,
            monitor: consumers.monitor,
        }
    }

    /// Attempts every cleanup even if another fails. Retry until both succeed
    /// before disposing of the product, or escalate a persistent device fault.
    pub fn shutdown(&mut self) -> ShutdownReport {
        self.monitor.stop();
        let recorder = self.recorder.stop(&mut self.recordings);
        let camera = self.camera.stop().map_err(Error::from);
        ShutdownReport { recorder, camera }
    }
}
