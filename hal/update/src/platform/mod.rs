//! Capability admission and OS factories; staging and lifetime policy stay shared.
#[cfg(any(hal_mock, test))]
pub(super) mod mock;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
#[allow(unsafe_code)]
mod nuttx;

#[cfg(any(hal_enabled, hal_mock))]
mod selected {
    #[cfg(hal_mock)]
    pub use super::mock::open;
    #[cfg(all(hal_enabled, target_os = "nuttx"))]
    pub use super::nuttx::open;
    pub use crate::controller::{Rejected, Stager};
}

#[cfg(any(hal_enabled, hal_mock))]
pub use selected::*;
