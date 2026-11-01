//! One bounded firmware upload on an already authenticated transport. The
//! receiver acknowledges RAM delivery only; the bootloader owns authentication.
#![forbid(unsafe_code)]
use std::io::{self, Read, Write};

pub const REQUEST: &[u8; 4] = b"NXU1";
pub const READY: &[u8; 4] = b"NXRD";
pub const RECEIVED: &[u8; 4] = b"NXOK";
pub const END: &[u8; 4] = b"DONE";
pub const COMMIT: &[u8; 4] = b"CMIT";

pub fn length(bytes: [u8; 4], maximum: u32) -> io::Result<u32> {
    let size = u32::from_be_bytes(bytes);
    if !(32..=maximum).contains(&size) {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    Ok(size)
}

fn read<R: Read>(
    io: &mut R,
    mut bytes: &mut [u8],
    check: &mut impl FnMut(&mut R) -> io::Result<()>,
) -> io::Result<()> {
    while !bytes.is_empty() {
        check(io)?;
        match io.read(bytes) {
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(n) => bytes = &mut bytes[n..],
            Err(e)
                if matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}
fn write<W: Write>(
    io: &mut W,
    mut bytes: &[u8],
    check: &mut impl FnMut(&mut W) -> io::Result<()>,
) -> io::Result<()> {
    while !bytes.is_empty() {
        check(io)?;
        match io.write(bytes) {
            Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
            Ok(n) => bytes = &bytes[n..],
            Err(e)
                if matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// REQUEST has already been consumed. `check` must enforce cancellation and a
/// whole-transfer deadline, refresh authentication leases and yield on retries.
pub fn receive<T: Read + Write>(
    io: &mut T,
    maximum: u32,
    mut check: impl FnMut(&mut T) -> io::Result<()>,
) -> io::Result<Vec<u8>> {
    let mut ready = [0; 8];
    ready[..4].copy_from_slice(READY);
    ready[4..].copy_from_slice(&maximum.to_be_bytes());
    write(io, &ready, &mut check)?;
    let mut size = [0; 4];
    read(io, &mut size, &mut check)?;
    let size = length(size, maximum)? as usize;
    let mut image = Vec::new();
    image
        .try_reserve_exact(size)
        .map_err(|_| io::Error::from(io::ErrorKind::OutOfMemory))?;
    image.resize(size, 0);
    read(io, &mut image, &mut check)?;
    let mut end = [0; 4];
    read(io, &mut end, &mut check)?;
    if &end != END {
        return Err(io::ErrorKind::InvalidData.into());
    }
    write(io, RECEIVED, &mut check)?;
    // Do not retire Wi-Fi until the sender has received the RAM receipt.
    read(io, &mut end, &mut check)?;
    if &end != COMMIT {
        return Err(io::ErrorKind::InvalidData.into());
    }
    Ok(image)
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Transport {
        input: io::Cursor<Vec<u8>>,
        output: Vec<u8>,
        calls: usize,
    }
    impl Read for Transport {
        fn read(&mut self, data: &mut [u8]) -> io::Result<usize> {
            self.calls += 1;
            if self.calls % 2 == 1 { return Err(io::ErrorKind::WouldBlock.into()); }
            let n = data.len().min(7);
            self.input.read(&mut data[..n])
        }
    }
    impl Write for Transport {
        fn write(&mut self, data: &[u8]) -> io::Result<usize> {
            self.calls += 1;
            if self.calls % 2 == 1 { return Err(io::ErrorKind::Interrupted.into()); }
            self.output.write(&data[..data.len().min(3)])
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    #[test]
    fn exact_delivery_bounds_truncation_and_cancellation() {
        let frame = [32u32.to_be_bytes().as_slice(), &[42; 32], END, COMMIT].concat();
        let mut malformed = frame.clone();
        *malformed.last_mut().unwrap() = b'X';
        for input in [
            frame.clone(),
            malformed,
            frame[..frame.len() - 1].to_vec(),
            [33u32.to_be_bytes().as_slice(), &[0; 32], END].concat(),
            [31u32.to_be_bytes().as_slice(), &[0; 31], END].concat(),
        ] {
            let mut io = Transport {
                input: io::Cursor::new(input.clone()),
                output: vec![],
                calls: 0,
            };
            let result = receive(&mut io, 32, |_| Ok(()));
            assert_eq!(result.is_ok(), input == frame);
            if input == frame {
                assert!(io.output.ends_with(RECEIVED));
            }
            if let Ok(image) = result {
                assert_eq!(image, [42; 32]);
            }
        }
        let mut io = Transport {
            input: io::Cursor::new(frame),
            output: vec![],
            calls: 0,
        };
        assert_eq!(
            receive(&mut io, 32, |_| Err(io::ErrorKind::TimedOut.into()))
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );
        assert!(io.output.is_empty());
    }
}
