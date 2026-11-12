use super::super::spi_resource;
use super::{DeviceError, Observed, Trace};

pub(crate) type Device = Observed<spi_resource::Device>;
pub(crate) type Resource = Device;

pub(crate) fn open(
    config: spi_resource::Config<'_>,
    trace: Trace,
) -> Result<Resource, DeviceError> {
    spi_resource::Device::open(config)
        .map(|device| trace.observe(device))
        .map_err(|_| DeviceError::Io)
}
