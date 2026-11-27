//! Fixed navigation telemetry protocol, bounded capture and synchronous delivery.
//! Applications own observation topology, workers, network choice and reconnects.
#![forbid(unsafe_code)]

mod clock;
pub mod collector;
mod endpoint;
pub mod export;
pub mod publication;
#[cfg(feature = "ble")]
pub mod radio;
mod snapshot;
pub mod wire;

pub use clock::ClockAnchor;
pub use endpoint::Endpoint;
pub use export::{Capture, CaptureBudget, CaptureConfig, CapturePublisher, CaptureView};
#[cfg(feature = "ble")]
pub use radio::{BleServices, BleStatus, BleView};
pub use snapshot::{reading_status, Reading, Snapshot};
pub use wire::write_usb;
