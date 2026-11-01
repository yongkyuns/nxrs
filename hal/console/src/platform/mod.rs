#[cfg(all(hal_enabled, target_os = "nuttx"))]
mod nuttx;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub use nuttx::*;
#[cfg(not(hal_enabled))]
mod unbound;
#[cfg(not(hal_enabled))]
pub use unbound::*;
