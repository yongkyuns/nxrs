//! Host-injected witnesses are separate from production module selection.
#[cfg(unix)]
#[path = "platform/nuttx/irq_tests.rs"]
mod irq_tests;
