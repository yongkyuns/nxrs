use std::io::{self, Read, Write};
struct Unavailable;
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
pub fn open() -> io::Result<impl Read + Write> {
    Err::<Unavailable, _>(io::ErrorKind::Unsupported.into())
}
