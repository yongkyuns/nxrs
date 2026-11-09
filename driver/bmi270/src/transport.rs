//! Chip register transport. Wire framing stays with the chip, bus ownership with its caller.
use super::Error;
use embedded_hal::{
    i2c::I2c,
    spi::{Operation, SpiDevice},
};

pub trait Registers {
    const WRITE_DELAY_NS: u32 = 0;
    fn read(&mut self, register: u8, bytes: &mut [u8]) -> Result<(), Error>;
    fn write(&mut self, register: u8, bytes: &[u8]) -> Result<(), Error>;
    fn initialize(&mut self) -> Result<(), Error> {
        Ok(())
    }
    fn read_reg(&mut self, register: u8) -> Result<u8, Error> {
        let mut data = [0];
        self.read(register, &mut data)?;
        Ok(data[0])
    }
    fn write_reg(&mut self, register: u8, value: u8) -> Result<(), Error> {
        self.write(register, &[value])
    }
}
pub struct I2cBus<B> {
    pub(super) bus: B,
    address: u8,
}
impl<B> I2cBus<B> {
    pub fn new(bus: B, address: u8) -> Result<Self, Error> {
        if !matches!(address, 0x68 | 0x69) {
            return Err(Error::InvalidData);
        }
        Ok(Self { bus, address })
    }
}
impl<B: I2c> Registers for I2cBus<B> {
    fn read(&mut self, register: u8, bytes: &mut [u8]) -> Result<(), Error> {
        self.bus
            .write_read(self.address, &[register], bytes)
            .map_err(|_| Error::Bus)
    }
    fn write(&mut self, register: u8, bytes: &[u8]) -> Result<(), Error> {
        if bytes.len() > 63 {
            return Err(Error::InvalidData);
        }
        let mut tx = [0; 64];
        tx[0] = register;
        tx[1..1 + bytes.len()].copy_from_slice(bytes);
        self.bus
            .write(self.address, &tx[..1 + bytes.len()])
            .map_err(|_| Error::Bus)
    }
}
pub struct SpiBus<S>(pub(super) S);
impl<S> SpiBus<S> {
    pub fn new(device: S) -> Self {
        Self(device)
    }
}
impl<S: SpiDevice> Registers for SpiBus<S> {
    const WRITE_DELAY_NS: u32 = 2_000;
    fn read(&mut self, register: u8, bytes: &mut [u8]) -> Result<(), Error> {
        self.0
            .transaction(&mut [
                Operation::Write(&[register | 0x80, 0]),
                Operation::Read(bytes),
            ])
            .map_err(|_| Error::Bus)
    }
    fn write(&mut self, register: u8, bytes: &[u8]) -> Result<(), Error> {
        self.0
            .transaction(&mut [
                Operation::Write(&[register & 0x7f]),
                Operation::Write(bytes),
            ])
            .map_err(|_| Error::Bus)
    }
    fn initialize(&mut self) -> Result<(), Error> {
        // The first read following power-on/reset selects the SPI interface.
        self.read_reg(0).map(|_| ())
    }
}

impl<B> TryFrom<(B, u8)> for I2cBus<B> {
    type Error = Error;
    fn try_from((bus, address): (B, u8)) -> Result<Self, Error> {
        Self::new(bus, address)
    }
}
impl<S> From<S> for SpiBus<S> {
    fn from(device: S) -> Self {
        Self::new(device)
    }
}
