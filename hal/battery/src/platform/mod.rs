cfg_if::cfg_if! {
    if #[cfg(platform = "nuttx")] {
        pub mod nuttx;
        pub use nuttx as selected;
    } else {
        pub mod unbound;
        pub use unbound as selected;
    }
}

pub use selected as construction;
