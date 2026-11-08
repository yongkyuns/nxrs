//! IMU diagnostic timing around the shared I²C resource.
use super::{resource, trace::Trace};
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
