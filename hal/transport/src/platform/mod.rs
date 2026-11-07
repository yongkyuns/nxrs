//! Explicit scripted fixtures; real UDP is selected by its Cargo functionality.
#[cfg(hal_provider = "mock")]
#[path = "mock.rs"]
mod selected;
#[cfg(hal_provider = "mock")]
pub use selected::MockTransport;
