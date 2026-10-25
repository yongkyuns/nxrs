//! Owner-task byte progress with one deadline across partial writes. No SDK,
//! worker, clock, sleeping policy or platform selection is owned here.
use std::io;
pub fn write_bytes(
    bytes: &[u8],
    mut send: impl FnMut(&[u8]) -> io::Result<usize>,
    mut expired: impl FnMut() -> bool,
    mut backoff: impl FnMut(),
) -> io::Result<()> {
    let mut offset = 0;
    while offset < bytes.len() {
        if expired() {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "record USB progress deadline",
            ));
        }
        match send(&bytes[offset..]) {
            Ok(n) if n > bytes.len() - offset => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "record USB count exceeds input",
                ))
            }
            Ok(0) => backoff(),
            Ok(n) => offset += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => backoff(),
            Err(e) => return Err(e),
        }
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn partial_and_zero_progress_retain_each_byte_exactly_once() {
        let mut output = Vec::new();
        let mut calls = 0;
        let mut waits = 0;
        write_bytes(
            b"0123456789",
            |remaining| {
                calls += 1;
                if calls == 2 {
                    return Ok(0);
                }
                let n = remaining.len().min(3);
                output.extend_from_slice(&remaining[..n]);
                Ok(n)
            },
            || false,
            || waits += 1,
        )
        .unwrap();
        assert_eq!(output, b"0123456789");
        assert_eq!(waits, 1);
    }
    #[test]
    fn silence_partial_failure_and_invalid_native_counts_never_report_success() {
        let clock = std::cell::Cell::new(0);
        let error = write_bytes(
            b"test",
            |_| Ok(0),
            || clock.get() >= 3,
            || clock.set(clock.get() + 1),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        let mut calls = 0;
        let error = write_bytes(
            b"test",
            |_| {
                calls += 1;
                if calls == 1 {
                    Ok(2)
                } else {
                    Err(io::Error::from(io::ErrorKind::BrokenPipe))
                }
            },
            || false,
            || {},
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::BrokenPipe);
        assert_eq!(
            write_bytes(b"test", |_| Ok(5), || false, || {})
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
    }
}
