#[derive(Clone, Copy, Debug)]
pub struct Config<'a> {
    pub path: &'a str,
    /// BSP selection index in NuttX's SPIDEV_USER namespace, not a GPIO number.
    pub chip_select: u16,
    pub frequency: u32,
    /// Standard SPI mode, 0 through 3; transactions are 8-bit, MSB first.
    pub mode: u8,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidConfig,
    Unsupported,
    Io,
}
impl embedded_hal::spi::Error for Error {
    fn kind(&self) -> embedded_hal::spi::ErrorKind {
        embedded_hal::spi::ErrorKind::Other
    }
}
