//! Select one implementation from the validated profile binding.
cfg_if::cfg_if! {
    if #[cfg(hal_provider = "linux")] {
        #[allow(unsafe_code)]
        mod linux;
        use linux as selected;
    } else if #[cfg(hal_provider = "macos")] {
        #[allow(unsafe_code)]
        mod macos;
        use macos as selected;
    } else if #[cfg(hal_provider = "nuttx")] {
        #[allow(unsafe_code)]
        mod nuttx;
        use nuttx as selected;
    } else {
        mod unbound;
        use unbound as selected;
    }
}

pub(crate) use selected::{open, open_quiescence};
