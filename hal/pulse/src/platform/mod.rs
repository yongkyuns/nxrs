cfg_if::cfg_if! {
    if #[cfg(platform = "nuttx")] {
        mod nuttx;
        use nuttx as selected;
    } else if #[cfg(platform = "mock")] {
        mod mock;
        use mock as selected;
    } else {
        mod unbound;
        use unbound as selected;
    }
}
pub use selected::open;
