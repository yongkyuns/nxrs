//! Select one checked-in implementation from the resolved profile.
#[cfg(all(hal_provider = "hardware", target_os = "nuttx"))]
use {::gpio as gpio_resource, acquisition::nuttx as events};

#[cfg(hal_provider = "mock")]
pub mod mock;
#[cfg(any(all(hal_provider = "hardware", target_os = "nuttx"), all(test, unix)))]
pub mod nuttx;
#[cfg(hal_provider = "simulation")]
pub mod simulation;
#[cfg(not(any(
    hal_provider = "hardware",
    hal_provider = "mock",
    hal_provider = "simulation"
)))]
pub mod unbound;

#[cfg(hal_provider = "mock")]
pub use mock as selected;
#[cfg(all(hal_provider = "hardware", target_os = "nuttx"))]
pub use nuttx as selected;
#[cfg(hal_provider = "simulation")]
pub use simulation as selected;
#[cfg(not(any(
    hal_provider = "hardware",
    hal_provider = "mock",
    hal_provider = "simulation"
)))]
pub use unbound as selected;

pub use selected as construction;
