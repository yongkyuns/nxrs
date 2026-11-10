//! Explicit scripted fixtures; real UDP is selected by its Cargo functionality.
cfg_if::cfg_if! {
    if #[cfg(hal_provider = "mock")] {
        mod mock;
        use mock as selected;
        pub use selected::MockTransport;
    }
}
