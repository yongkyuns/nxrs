//! Profile-named GNSS devices; services receive normalized `gnss::Device` references.
#![forbid(unsafe_code)]
cfg_if::cfg_if! {
    if #[cfg(platform = "nuttx")] {
        mod nuttx;
        use nuttx as selected;
    } else {
        use gnss::construction as selected;
    }
}
pub use selected::devices::*;
