//! Explicit scripted fixtures; real UDP is selected by its Cargo functionality.
#[cfg(hal_mock)]
#[path = "mock.rs"]
mod selected;
#[cfg(hal_mock)]
pub use selected::MockTransport;
