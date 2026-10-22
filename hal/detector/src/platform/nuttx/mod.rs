//! NuttX acquisition selects a model; only PICO needs a native context lease.
#[cfg(feature = "pico-model")]
mod pico;
#[cfg(not(feature = "pico-model"))]
pub(crate) use crate::model::open;
#[cfg(feature = "qualification")]
pub(crate) use pico::kernel;
#[cfg(feature = "pico-model")]
pub(crate) use pico::open;
