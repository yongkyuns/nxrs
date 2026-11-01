//! Product composition root. Capabilities and reusable behavior stay in their crates.
#![deny(unsafe_code)]

#[cfg(any(target_os = "nuttx", test))]
mod commissioning;
#[cfg(target_os = "nuttx")]
mod platform;
#[cfg(target_os = "nuttx")]
mod runtime;
#[cfg(target_os = "nuttx")]
mod model {
    include!(concat!(env!("OUT_DIR"), "/detector_budget.rs"));
}

pub fn run() -> Result<(), String> {
    #[cfg(target_os = "nuttx")]
    return runtime::run();
    #[cfg(not(target_os = "nuttx"))]
    Err("camera product requires NuttX".into())
}
