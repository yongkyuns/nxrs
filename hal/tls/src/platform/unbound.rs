//! Admission without a selected backend: public contracts remain usable, but
//! no SDK, entropy source or connection can be acquired.
use crate::{Certificates, Error, TimeLease};
use std::io::{self, Read, Write};
use std::marker::PhantomData;
use std::net::TcpStream;
use std::rc::Rc;

pub struct Identity {
    _private: PhantomData<Rc<()>>,
}
pub struct Connection<'a> {
    _identity: &'a mut Identity,
}
pub fn open() -> Result<Identity, Error> {
    Err(Error::Unsupported)
}
impl Identity {
    pub fn request(&mut self, _: &str, output: &mut [u8]) -> Result<usize, Error> {
        output.fill(0);
        Err(Error::Unsupported)
    }
    pub fn refresh_entropy(&self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    pub fn validate_certificates(&self, _: Certificates<'_>, _: TimeLease) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    pub fn accept<'a>(
        &'a mut self,
        _: TcpStream,
        _: Certificates<'_>,
        _: TimeLease,
    ) -> Result<Connection<'a>, Error> {
        Err(Error::Unsupported)
    }
}
impl Connection<'_> {
    pub fn poll(&mut self) -> Result<bool, Error> {
        Err(Error::Unsupported)
    }
    pub fn refresh_time(&mut self, _: TimeLease) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    pub fn close(&mut self) -> Result<bool, Error> {
        Err(Error::Unsupported)
    }
}
impl Read for Connection<'_> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        output.fill(0);
        Err(io::ErrorKind::Unsupported.into())
    }
}
impl Write for Connection<'_> {
    fn write(&mut self, _: &[u8]) -> io::Result<usize> {
        Err(io::ErrorKind::Unsupported.into())
    }
    fn flush(&mut self) -> io::Result<()> {
        Err(io::ErrorKind::Unsupported.into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unbound_acquisition_refuses_without_a_native_backend() {
        fn stream<T: Read + Write>() {}
        stream::<Connection<'_>>();
        assert!(matches!(crate::open(), Err(Error::Unsupported)));
    }
}
