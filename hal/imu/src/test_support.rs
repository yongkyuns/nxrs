//! Exercise NuttX acquisition with fake resources on Unix hosts.
#[cfg(all(unix, not(platform = "nuttx")))]
#[path = "platform/nuttx/mod.rs"]
mod nuttx;
