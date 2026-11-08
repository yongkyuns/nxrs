//! Concrete resource construction and chip-specific qualification controls.
//! Device normalization is implemented directly by each driver.
use super::{device::Delay, observed_bus::Bus};
use peripheral::imu::{Error, Imu};

pub(super) trait Construct: Imu + Sized {
    fn open(bus: Bus, address: u8, settings: Self::Settings) -> Result<Self, Error>;
    fn enable_sensor_reference(&mut self) -> Result<(), Error>;
}

impl Construct for nxrs_bmi270::Bmi270<Bus, Delay> {
    fn open(bus: Bus, address: u8, settings: Self::Settings) -> Result<Self, Error> {
        Self::open_configured(bus, address, Delay, settings).map_err(Into::into)
    }

    fn enable_sensor_reference(&mut self) -> Result<(), Error> {
        Self::enable_sensor_reference(self).map_err(Into::into)
    }
}

impl Construct for nxrs_lsm6dso::Lsm6dso<Bus> {
    fn open(bus: Bus, address: u8, settings: Self::Settings) -> Result<Self, Error> {
        Self::open_configured(bus, address, Delay, settings).map_err(Into::into)
    }

    fn enable_sensor_reference(&mut self) -> Result<(), Error> {
        Self::enable_sensor_reference(self).map_err(Into::into)
    }
}
