//! Host-injected provider witnesses. Never part of production selection.

#[allow(dead_code, unused_imports)]
#[path = "platform/mock.rs"]
pub(crate) mod mock_tests;

#[cfg(all(unix, not(target_os = "nuttx")))]
#[allow(dead_code, unused_imports)]
#[path = "platform/nuttx/mod.rs"]
mod nuttx_tests;
