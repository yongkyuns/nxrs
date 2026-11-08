//! NuttX descriptor readiness operations shared by acquisition providers.
//! `wait` returns true for device readiness and false for cancellation; `tick`
//! returns true for timeout and false for cancellation. Cancellation wins when
//! both are reported. Callers own retry/deadline policy; errors remain explicit.
#[allow(unsafe_code)]
mod ffi;
pub use ffi::{pipe, tick, wait};
