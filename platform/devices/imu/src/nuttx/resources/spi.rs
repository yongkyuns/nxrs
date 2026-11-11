use crate::spi_resource;
use acquisition::probe::{Observed, Trace};
use imu::DeviceError;

pub(crate) type Spi = Observed<spi_resource::Device>;
pub(crate) type SpiResource = Spi;

pub(crate) struct SpiBinding<'a> {
    pub(crate) path: &'a str,
    pub(crate) chip_select: u16,
    pub(crate) frequency: u32,
    pub(crate) mode: u8,
    pub(crate) irq_path: Option<&'a str>,
}
impl SpiBinding<'_> {
    pub(crate) fn open(&self, trace: Trace) -> Result<Spi, DeviceError> {
        spi_resource::Device::open(spi_resource::Config {
            path: self.path,
            chip_select: self.chip_select,
            frequency: self.frequency,
            mode: self.mode,
        })
        .map(|device| trace.observe(device))
        .map_err(|_| DeviceError::Io)
    }
}
