//! Only the selected OS supplies the detector factory.
#[cfg_attr(target_os = "linux", path = "linux/mod.rs")]
#[cfg_attr(target_os = "macos", path = "macos/mod.rs")]
#[cfg_attr(target_os = "windows", path = "windows/mod.rs")]
#[cfg_attr(target_os = "nuttx", path = "nuttx/mod.rs")]
mod selected;
#[cfg(detector_pico)]
pub(crate) use selected::kernel;
pub(crate) use selected::open;
