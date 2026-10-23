//! Shared model execution needs no OS-specific compute-context lease.
#[cfg(all(feature = "probe", feature = "pico-model"))]
pub(crate) use crate::model::kernel;
pub(crate) use crate::model::open;
