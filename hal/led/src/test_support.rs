//! Host-injected providers; never part of production selection.
#[cfg(all(unix, not(target_os = "nuttx")))]
#[allow(dead_code, unused_imports, unsafe_code)]
#[path = "platform/nuttx/mod.rs"]
mod nuttx_tests;

#[cfg(not(platform = "mock"))]
#[allow(dead_code, unused_imports)]
#[path = "platform/mock.rs"]
mod mock_tests;
