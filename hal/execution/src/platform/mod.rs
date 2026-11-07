//! Select one implementation from profile admission and Rust target_os.
#[cfg_attr(
    all(hal_provider = "hardware", target_os = "nuttx"),
    path = "nuttx/mod.rs"
)]
#[cfg_attr(not(hal_provider = "hardware"), path = "unbound.rs")]
mod selected;
pub use selected::configure_current;
