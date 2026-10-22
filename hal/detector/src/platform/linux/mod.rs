//! Shared model execution needs no OS-specific compute-context lease.
#[cfg(detector_pico)]
pub(crate) use crate::model::kernel;
pub(crate) use crate::model::open;
