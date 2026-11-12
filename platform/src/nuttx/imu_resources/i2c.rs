use super::super::resource;
use super::{DeviceError, Observed, Trace};

pub(crate) type Bus = Observed<resource::Bus>;
pub(crate) type Resource = (Bus, u8);

pub(crate) fn open(
    (config, address): (resource::Config<'_>, u8),
    trace: Trace,
) -> Result<Resource, DeviceError> {
    resource::Bus::open(config)
        .map(|bus| (trace.observe(bus), address))
        .map_err(|_| DeviceError::Io)
}
