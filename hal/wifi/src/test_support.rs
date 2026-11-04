//! Host-injected NuttX source used by ownership and worker tests.
#[cfg(all(unix, not(target_os = "nuttx")))]
#[allow(dead_code, unused_imports)]
#[path = "platform/nuttx/mod.rs"]
pub(crate) mod nuttx_tests;
