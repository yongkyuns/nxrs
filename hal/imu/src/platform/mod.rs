//! Select one implementation from the validated profile binding.
cfg_if::cfg_if! {
    if #[cfg(hal_provider = "nuttx")] {
        #[cfg(not(test))]
        use {::gpio as gpio_resource, acquisition::nuttx as events};
        pub mod nuttx;
        pub use nuttx as selected;
    } else if #[cfg(hal_provider = "mock")] {
        pub mod mock;
        pub use mock as selected;
    } else if #[cfg(hal_provider = "simulation")] {
        pub mod simulation;
        pub use simulation as selected;
    } else {
        pub mod unbound;
        pub use unbound as selected;
    }
}

pub use selected as construction;
