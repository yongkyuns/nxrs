//! Host entry points for the diagnostic facade.
#![deny(unsafe_code)]

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
pub mod browser;
#[cfg(target_os = "nuttx")]
pub mod nuttx;
