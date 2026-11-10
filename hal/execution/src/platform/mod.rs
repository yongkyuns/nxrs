//! Select one implementation from the validated profile binding.
cfg_if::cfg_if! {
    if #[cfg(hal_provider = "nuttx")] {
        mod nuttx;
        use nuttx as selected;
    } else {
        mod unbound;
        use unbound as selected;
    }
}

pub use selected::configure_current;
