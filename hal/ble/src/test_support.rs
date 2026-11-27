//! Host-injected provider tests are separate from production selection.
#[cfg(all(unix, not(target_os = "nuttx")))]
#[allow(dead_code, unused_imports)]
#[path = "platform/nuttx/mod.rs"]
mod nuttx_tests;

// Exercise the production byte-stream accounting with task-context SDK fakes.
// This does not qualify the NuttX callback ABI or controller delivery timing.
#[cfg(all(feature = "peripheral", unix, not(target_os = "nuttx")))]
#[allow(dead_code, unused_imports, unsafe_code, unused_unsafe)]
#[path = "platform/nuttx/peripheral.rs"]
mod peripheral_tests;
