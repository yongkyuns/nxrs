//! Select one implementation from the validated profile binding.
#[cfg(any(platform = "nuttx", test))]
#[path = "../quiescence.rs"]
mod quiescence;
#[cfg(any(platform = "linux", platform = "macos", all(test, unix)))]
#[path = "../shared/mod.rs"]
mod shared;
#[cfg(test)]
#[path = "../test_support.rs"]
mod test_support;
#[cfg(any(
    platform = "nuttx",
    platform = "linux",
    platform = "macos",
    all(test, unix)
))]
#[path = "../worker/mod.rs"]
mod worker;

#[cfg(not(platform = "nuttx"))]
#[path = "../unsupported_quiescence.rs"]
mod unsupported_quiescence;
cfg_if::cfg_if! {
    if #[cfg(platform = "linux")] {
        #[allow(unsafe_code)]
        mod linux;
        use linux as selected;
    } else if #[cfg(platform = "macos")] {
        #[allow(unsafe_code)]
        mod macos;
        use macos as selected;
    } else if #[cfg(platform = "nuttx")] {
        #[allow(unsafe_code)]
        mod nuttx;
        use nuttx as selected;
    } else {
        mod unbound;
        use unbound as selected;
    }
}

pub(crate) use selected::{open, open_quiescence};
