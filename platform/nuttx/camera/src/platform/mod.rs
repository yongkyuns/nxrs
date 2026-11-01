//! NuttX product integration; no capability implementations live here.
mod nuttx;
pub(crate) use nuttx::{initialize_network, priority, Console};
