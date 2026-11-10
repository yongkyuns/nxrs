//! Admit a configured model; NuttX additionally owns its compute-context lease.
cfg_if::cfg_if! {
    if #[cfg(all(platform = "nuttx", any(feature = "person-model", feature = "pico-model")))] {
        mod nuttx;
        use nuttx as selected;
    } else if #[cfg(all(
        any(platform = "linux", platform = "macos", platform = "windows"),
        any(feature = "person-model", feature = "pico-model")
    ))] {
        use crate::model as selected;
    } else {
        mod unbound;
        use unbound as selected;
    }
}
pub use selected::open;

// Qualification retains the same admission and native compute-context rules.
#[cfg(all(feature = "probe", feature = "pico-model"))]
pub(crate) use selected::kernel;
