//! Shared camera/recording/telemetry composition for qualification only.
//! Product firmware composes its own services; it must not depend on this crate.
#![no_std]
#![forbid(unsafe_code)]

mod reader;
mod error;
pub mod recorder;
pub mod monitor;
pub mod product;

pub use recorder::Recorder;
pub use monitor::Monitor;
pub use error::Error;
pub use product::{CameraProduct, ConsumerReport, ShutdownReport, TickReport};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum AppState {
    #[default]
    Stopped,
    Running,
    Stopping,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct AppStats { pub processed: u64, pub skipped: u64 }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Progress { Idle, Processed { sequence: u64, skipped: u64 } }
