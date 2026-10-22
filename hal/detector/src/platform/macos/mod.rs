//! Shared model execution needs no OS-specific compute-context lease.
#[cfg(feature = "qualification")]
pub(crate) use crate::model::kernel;
pub(crate) use crate::model::open;
