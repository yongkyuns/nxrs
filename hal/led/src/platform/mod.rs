//! Select one implementation from the validated profile binding.
cfg_if::cfg_if! {
    if #[cfg(hal_provider = "nuttx")] {
        #[allow(unsafe_code)]
        mod nuttx;
        use nuttx as selected;
        pub use selected::UserLeds;
    } else if #[cfg(hal_provider = "mock")] {
        mod mock;
        use mock as selected;
        pub use selected::MockLeds;
    } else {
        mod unbound;
        use unbound as selected;
    }
}

pub use selected::open;
