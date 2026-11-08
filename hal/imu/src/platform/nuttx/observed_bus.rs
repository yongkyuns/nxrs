//! IMU diagnostic timing around the shared I²C/SPI resources.
use super::{resource, spi_resource, trace::Trace};
use crate::DeviceError;
use embedded_hal::i2c::{ErrorType, I2c, Operation};

pub(super) struct Bus {
    inner: resource::Bus,
    trace: Trace,
}
impl Bus {
    pub(super) fn open(path: &str, frequency: u32, trace: Trace) -> Result<Self, DeviceError> {
        let inner = resource::Bus::open(resource::Config { path, frequency })
            .map_err(|_| DeviceError::Io)?;
        Ok(Self { inner, trace })
    }
}
impl ErrorType for Bus {
    type Error = <resource::Bus as ErrorType>::Error;
}
impl I2c for Bus {
    fn transaction(
        &mut self,
        address: u8,
        operations: &mut [Operation<'_>],
    ) -> Result<(), Self::Error> {
        self.trace
            .ioctl(|| self.inner.transaction(address, operations))
    }
}

pub(super) struct Spi {
    inner: spi_resource::Device,
    trace: Trace,
}
impl Spi {
    pub(super) fn open(
        config: spi_resource::Config<'_>,
        trace: Trace,
    ) -> Result<Self, DeviceError> {
        Ok(Self {
            inner: spi_resource::Device::open(config).map_err(|_| DeviceError::Io)?,
            trace,
        })
    }
}
impl embedded_hal::spi::ErrorType for Spi {
    type Error = <spi_resource::Device as embedded_hal::spi::ErrorType>::Error;
}
impl embedded_hal::spi::SpiDevice for Spi {
    fn transaction(
        &mut self,
        operations: &mut [embedded_hal::spi::Operation<'_, u8>],
    ) -> Result<(), Self::Error> {
        self.trace.ioctl(|| self.inner.transaction(operations))
    }
}
