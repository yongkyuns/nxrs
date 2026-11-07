//! Select one implementation from profile admission and Rust target_os.
#[cfg_attr(
    all(hal_provider = "hardware", target_os = "linux"),
    path = "linux/mod.rs"
)]
#[cfg_attr(
    all(hal_provider = "hardware", target_os = "macos"),
    path = "macos/mod.rs"
)]
#[cfg_attr(
    all(hal_provider = "hardware", target_os = "nuttx"),
    path = "nuttx/mod.rs"
)]
#[allow(unsafe_code)]
mod selected;
pub(crate) use selected::{gatt_driver, open};
