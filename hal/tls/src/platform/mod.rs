//! Select one implementation from the validated profile binding.
cfg_if::cfg_if! {
    if #[cfg(hal_provider = "linux")] {
        mod linux;
        use linux as selected;
        #[cfg(test)]
        pub(crate) use selected::Source;
    } else if #[cfg(hal_provider = "macos")] {
        mod macos;
        use macos as selected;
        #[cfg(test)]
        pub(crate) use selected::Source;
    } else if #[cfg(hal_provider = "nuttx")] {
        mod nuttx;
        use nuttx as selected;
        #[cfg(test)]
        pub(crate) use selected::Source;
    } else {
        mod unbound;
        use unbound as selected;
    }
}

pub use selected::{open, Connection, Identity};
