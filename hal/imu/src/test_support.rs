//! Exercise NuttX acquisition with fake resources on Unix hosts.
#[cfg(all(unix, not(hal_provider = "nuttx")))]
#[path = "platform/nuttx/mod.rs"]
mod nuttx;
