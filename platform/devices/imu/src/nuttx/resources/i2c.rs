use crate::resource;
use acquisition::probe::{Observed, Trace};
use imu::DeviceError;

pub(crate) type Bus = Observed<resource::Bus>;
pub(crate) type I2cResource = (Bus, u8);

pub(crate) struct I2cBinding<'a> {
    pub(crate) path: &'a str,
    pub(crate) address: u8,
    pub(crate) frequency: u32,
    pub(crate) irq_path: Option<&'a str>,
}

impl I2cBinding<'_> {
    pub(crate) fn open(&self, trace: Trace) -> Result<(Bus, u8), DeviceError> {
        let bus = resource::Bus::open(resource::Config {
            path: self.path,
            frequency: self.frequency,
        })
        .map_err(|_| DeviceError::Io)?;
        Ok((trace.observe(bus), self.address))
    }
}
