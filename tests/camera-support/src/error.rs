//! Errors from composing the independent services into the example product.

use camera_service::Error as CameraError;
use crate::recording::Error as RecordingError;
use crate::packet_summary::Error as TelemetryError;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    AlreadyRunning,
    Camera(CameraError),
    Recording(RecordingError),
    Telemetry(TelemetryError),
}

impl From<CameraError> for Error {
    fn from(value: CameraError) -> Self { Self::Camera(value) }
}

impl From<RecordingError> for Error {
    fn from(value: RecordingError) -> Self { Self::Recording(value) }
}

impl From<TelemetryError> for Error {
    fn from(value: TelemetryError) -> Self { Self::Telemetry(value) }
}
