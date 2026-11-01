//! NuttX product integration; no capability implementations live here.
mod nuttx;
pub(crate) use nuttx::priority;
pub use nuttx::{initialize_network, Console};
