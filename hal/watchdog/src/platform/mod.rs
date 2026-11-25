//! Select one implementation from the validated profile binding.
cfg_if::cfg_if! {
    if #[cfg(platform = "nuttx")] {
        mod nuttx;
        use nuttx as selected;
    } else {
        mod unbound;
        use unbound as selected;
    }
}

pub use selected::{adopt, arm};
