//! Admission without a selected backend: public contracts remain usable, but
//! no SDK, entropy source or connection can be acquired.
use crate::{Certificates, Error, Stream, TimeLease};
use std::io::{self, Read, Write};
use std::marker::PhantomData;
use std::net::TcpStream;
use std::rc::Rc;

pub struct Identity {
    _private: PhantomData<Rc<()>>,
}
pub struct Connection<'a, T: Stream = TcpStream> {
    _transport: PhantomData<T>,
    _identity: &'a mut Identity,
}
pub fn open() -> Result<Identity, Error> {
    Err(Error::Unsupported)
}
impl Identity {
    /// A profile without a TLS provider cannot open pinned client sessions.
    pub const SUPPORTS_PINNED_CLIENT: bool = false;

    pub fn request(&self, _: &str, output: &mut [u8]) -> Result<usize, Error> {
        output.fill(0);
        Err(Error::Unsupported)
    }
    pub fn refresh_entropy(&self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    pub fn configure(&mut self, _: Certificates<'_>, _: &TimeLease) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    pub fn renew(
        &self,
        _: Certificates<'_>,
        _: &TimeLease,
        _: impl FnMut() -> Result<(), Error>,
    ) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    pub fn accept<'a>(&'a self, _: TcpStream, _: TimeLease) -> Result<Connection<'a>, Error> {
        Err(Error::Unsupported)
    }
    #[cfg(feature = "pairing")]
    pub fn pairing_key(&self) -> Result<[u8; 32], Error> {
        Err(Error::Unsupported)
    }
    #[cfg(feature = "pairing")]
    pub fn accept_pairing<'a, T: Stream>(
        &'a self,
        _: T,
        _: &[u8; 32],
    ) -> Result<Connection<'a, T>, Error> {
        Err(Error::Unsupported)
    }
    pub fn accept_stream<'a, T: Stream>(
        &'a self,
        _: T,
        _: TimeLease,
    ) -> Result<Connection<'a, T>, Error> {
        Err(Error::Unsupported)
    }
    pub fn connect_pinned(
        &self,
        _: TcpStream,
        _: [u8; 32],
        _: std::time::Duration,
    ) -> Result<Connection<'_>, Error> {
        Err(Error::Unsupported)
    }
}
impl<T: Stream> Connection<'_, T> {
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
impl<T: Stream> Read for Connection<'_, T> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        output.fill(0);
        Err(io::ErrorKind::Unsupported.into())
    }
}
impl<T: Stream> Write for Connection<'_, T> {
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
