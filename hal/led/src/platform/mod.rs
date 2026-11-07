//! Select one implementation from profile admission and Rust target_os.
#[cfg_attr(
    all(hal_provider = "hardware", target_os = "nuttx"),
    path = "nuttx/mod.rs"
)]
#[cfg_attr(hal_provider = "mock", path = "mock.rs")]
#[cfg_attr(
    not(any(hal_provider = "hardware", hal_provider = "mock")),
    path = "unbound.rs"
)]
#[cfg_attr(
    all(hal_provider = "hardware", target_os = "nuttx"),
    allow(unsafe_code)
)]
mod selected;
pub use selected::open;
#[cfg(hal_provider = "mock")]
pub use selected::MockLeds;
#[cfg(all(hal_provider = "hardware", target_os = "nuttx"))]
pub use selected::UserLeds;
