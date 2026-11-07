#[cfg(any(target_os = "nuttx", target_os = "none"))]
mod native;
#[cfg(target_os = "nuttx")]
mod nuttx;
#[cfg(target_os = "nuttx")]
pub use nuttx::*;
#[cfg(target_os = "none")]
mod zephyr;
#[cfg(target_os = "none")]
pub use zephyr::*;
#[cfg(not(any(target_os = "nuttx", target_os = "none")))]
mod host;
#[cfg(not(any(target_os = "nuttx", target_os = "none")))]
pub use host::*;
