use crate::peripheral::{Link, Listener, Service};
use crate::Error;
use std::io::{self, Read, Write};

pub(crate) fn open(_: Service, _: Option<Service>) -> Result<impl Listener, Error> {
    Err::<Unavailable, _>(Error::Unsupported)
}

struct Unavailable;
impl Listener for Unavailable {
    type Transport = Self;
    fn accept(&mut self) -> Result<Option<Self::Transport>, Error> {
        Ok(None::<Self>)
    }
}
impl Read for Unavailable {
    fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
        Err(io::ErrorKind::Unsupported.into())
    }
}
impl Write for Unavailable {
    fn write(&mut self, _: &[u8]) -> io::Result<usize> {
        Err(io::ErrorKind::Unsupported.into())
    }
    fn flush(&mut self) -> io::Result<()> {
        Err(io::ErrorKind::Unsupported.into())
    }
}
impl Link for Unavailable {
    fn abort(&mut self) {}
}
