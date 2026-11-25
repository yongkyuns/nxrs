//! Host-injected provider tests; never part of production selection.
#[cfg(all(unix, not(target_os = "nuttx")))]
#[allow(dead_code, unused_imports)]
#[path = "platform/nuttx/mod.rs"]
mod nuttx_tests;
