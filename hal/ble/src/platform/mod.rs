//! Select one implementation from the validated profile binding.
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
    }
}

pub(crate) use selected::{gatt_driver, open};
