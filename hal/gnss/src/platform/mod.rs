//! Select one implementation from the validated profile binding.
cfg_if::cfg_if! {
    if #[cfg(platform = "nuttx")] {
        pub mod nuttx;
        pub use nuttx as selected;
    } else if #[cfg(platform = "mock")] {
        pub mod mock;
        pub use mock as selected;
    } else if #[cfg(platform = "simulation")] {
        pub mod simulation;
        pub use simulation as selected;
    } else {
        pub mod unbound;
        pub use unbound as selected;
    }
}
pub use selected as construction;
