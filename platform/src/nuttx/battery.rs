//! Construct the battery gauge from the selected profile's I²C resource.
use ::battery::{construction::Instance, Device, Error};
use ::i2c as resource;
use ::i2c as resources;

macro_rules! declare_device {
    ($name:ident, $_id:literal, $driver:ty, $_resource:ty, $_open:path, $binding:expr, $_irq:expr, $_stack:expr, $_reference:tt) => {
        static $name: Instance<$driver, (resource::Config<'static>, u8)> =
            Instance::new($binding, |(config, address)| {
                let bus = resource::Bus::open(config).map_err(|_| Error::Io)?;
                Ok(<$driver>::new(bus, address))
            });
    };
}

include!(concat!(env!("OUT_DIR"), "/battery_config.rs"));
