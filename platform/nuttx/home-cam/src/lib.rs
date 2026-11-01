//! NuttX startup and execution resources for the home camera application.
#![deny(unsafe_code)]

#[cfg(target_os = "nuttx")]
mod platform;
#[cfg(target_os = "nuttx")]
pub use platform::{initialize_network, Console};

mod budget {
    include!(concat!(env!("OUT_DIR"), "/detector_budget.rs"));
}

#[derive(Clone, Copy)]
pub struct DetectorBudget {
    pub stack_bytes: usize,
    pub sample_ms: u64,
}
#[derive(Clone, Copy)]
pub struct Resources {
    pub detector: Option<DetectorBudget>,
    pub tls_stack_bytes: usize,
    pub tls_poll_interval: std::time::Duration,
    pub update_capacity: usize,
}
pub fn resources() -> Resources {
    Resources {
        detector: budget::ENABLED.then_some(DetectorBudget {
            stack_bytes: budget::STACK_BYTES,
            sample_ms: budget::SAMPLE_MS,
        }),
        tls_stack_bytes: 64 * 1024,
        tls_poll_interval: std::time::Duration::from_millis(2),
        update_capacity: 0x200000 - 8192,
    }
}

pub enum Worker {
    Tls,
    Detector,
}
#[cfg(target_os = "nuttx")]
pub fn initialize_worker(worker: Worker) -> std::io::Result<()> {
    platform::priority(match worker {
        Worker::Tls => 80,
        Worker::Detector => 90,
    })
}
