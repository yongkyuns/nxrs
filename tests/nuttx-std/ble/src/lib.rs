//! Shared device scenarios, not a production Bluetooth service.
#![forbid(unsafe_code)]

mod scenario;
#[cfg(target_os = "nuttx")]
pub use scenario::run;
pub use scenario::{run_with, Mode, Progress, ScanSink, WakeContext, CAPACITY, SERVICE};
