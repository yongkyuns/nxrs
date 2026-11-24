//! Connect a BLE HAL byte stream to the common TLS implementation.
use ::ble::peripheral::Link;
use std::io::{self, Read, Write};

pub fn accept<'a, L: Link>(
    identity: &'a tls::Identity,
    link: L,
    key: &[u8; 32],
) -> Result<tls::Connection<'a, Stream<L>>, tls::Error> {
    identity.accept_pairing(Stream(link), key)
}

pub struct Stream<T>(T);
impl<T: Link> Read for Stream<T> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.0.read(buf)
    }
}
impl<T: Link> Write for Stream<T> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.0.flush()
    }
}
impl<T: Link> tls::Stream for Stream<T> {
    fn prepare(&mut self) -> io::Result<()> {
        Ok(())
    }
    fn abort(&mut self) {
        self.0.abort();
    }
}
