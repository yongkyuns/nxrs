//! Select one checked-in implementation from the resolved profile.
#[cfg_attr(
    all(hal_provider = "hardware", target_os = "nuttx"),
    path = "nuttx/mod.rs"
)]
#[cfg_attr(hal_provider = "mock", path = "mock.rs")]
#[cfg_attr(hal_provider = "simulation", path = "simulation.rs")]
#[cfg_attr(
    not(any(
        hal_provider = "hardware",
        hal_provider = "mock",
        hal_provider = "simulation"
    )),
    path = "unbound.rs"
)]
mod selected;
pub use selected::open;
