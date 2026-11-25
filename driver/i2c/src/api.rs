//! Controller configuration; device addresses belong to individual transactions.
#![forbid(unsafe_code)]
#[derive(Clone, Copy, Debug)]
pub struct Config<'a> {
    pub path: &'a str,
    pub frequency: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidConfig,
    Unsupported,
    Io,
}
impl embedded_hal::i2c::Error for Error {
    fn kind(&self) -> embedded_hal::i2c::ErrorKind {
        embedded_hal::i2c::ErrorKind::Other
    }
}
