//! Select one implementation from the validated profile binding.
cfg_if::cfg_if! {
    if #[cfg(platform = "nuttx")] {
        mod nuttx;
        use nuttx as selected;
    } else if #[cfg(platform = "mock")] {
        mod mock;
        use mock as selected;
        pub use selected::{CameraAction, MockCamera, FORMAT};
    } else {
        mod unbound;
        use unbound as selected;
    }
}

pub(super) use selected::open;
