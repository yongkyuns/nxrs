//! Select one implementation from the validated profile binding.
#[cfg(all(platform = "nuttx", not(test)))]
use {::gpio as gpio_resource, acquisition::nuttx as events};

#[cfg(platform = "nuttx")]
#[path = "nuttx/mod.rs"]
pub mod selected;

#[cfg(platform = "mock")]
#[path = "mock.rs"]
pub mod selected;

#[cfg(platform = "simulation")]
#[path = "simulation.rs"]
pub mod selected;

#[cfg(not(any(platform = "nuttx", platform = "mock", platform = "simulation")))]
#[path = "unbound.rs"]
pub mod selected;

pub use selected as construction;
