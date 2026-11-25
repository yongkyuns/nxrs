//! Profile-selected devices. Applications pass normalized HAL references to services.
#![forbid(unsafe_code)]

#[cfg(feature = "imu")]
cfg_if::cfg_if! {
    if #[cfg(device_binding = "imu:nuttx")] {
        #[cfg(feature = "i2c")]
        use ::i2c as resource;
        #[cfg(feature = "spi")]
        use ::spi as spi_resource;
        #[path = "nuttx/imu.rs"]
        mod imu_factory;
        pub use imu_factory::devices as imu;
    } else {
        pub use ::imu::construction::devices as imu;
    }
}

#[cfg(feature = "gnss")]
cfg_if::cfg_if! {
    if #[cfg(device_binding = "gnss:nuttx")] {
        #[path = "nuttx/gnss.rs"]
        mod gnss_factory;
        pub use gnss_factory::devices as gnss;
    } else {
        pub use ::gnss::construction::devices as gnss;
    }
}

#[cfg(feature = "probe")]
pub mod probe;

#[cfg(feature = "battery")]
cfg_if::cfg_if! {
    if #[cfg(device_binding = "battery:nuttx")] {
        #[path = "nuttx/battery.rs"]
        mod battery_factory;
        pub use battery_factory::devices as battery;
    } else {
        pub use ::battery::construction::devices as battery;
    }
}
