//! Driver observations only; never prints samples or enables TLS admission.
#![cfg_attr(not(target_os = "nuttx"), allow(dead_code))]
use std::io::{ErrorKind, Read};
use std::time::Instant;

#[derive(Default, Debug)]
struct Observed {
    reads: usize,
    bytes: usize,
    unavailable: usize,
    max_us: u128,
}

fn observe(source: &mut impl Read, active: bool) -> std::io::Result<Observed> {
    let mut result = Observed::default();
    for _ in 0..32 {
        for size in [1, 3, 4, 31, 32, 33, 65] {
            let mut buffer = [0xa5; 65];
            let start = Instant::now();
            let read = source.read(&mut buffer[..size]);
            result.max_us = result.max_us.max(start.elapsed().as_micros());
            match read {
                Ok(length)
                    if active
                        && length > 0
                        && length <= size.min(32)
                        && buffer[length..].iter().all(|byte| *byte == 0xa5) =>
                {
                    result.reads += 1;
                    result.bytes += length;
                }
                Err(error)
                    if error.kind() == ErrorKind::WouldBlock
                        && buffer.iter().all(|byte| *byte == 0xa5) =>
                {
                    result.unavailable += 1
                }
                _ => {
                    return Err(std::io::Error::other(
                        "unexpected entropy read or buffer overwrite",
                    ))
                }
            }
        }
    }
    if active && result.reads == 0 {
        return Err(std::io::Error::other("no active RF samples observed"));
    }
    Ok(result)
}

#[cfg(target_os = "nuttx")]
fn main() -> std::io::Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    unsafe extern "C" {
        fn entropy_probe_open_flags() -> i32;
    }
    let mut args = std::env::args().skip(1);
    let mode = args.next().unwrap_or_default();
    if !matches!(mode.as_str(), "cold" | "active" | "stopped") || args.next().is_some() {
        return Err(std::io::Error::other("expected cold, active or stopped"));
    }
    // SAFETY: fixture-only fcntl.h constant, with no retained state or pointer.
    let flags = unsafe { entropy_probe_open_flags() };
    let mut source = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(flags)
        .open("/dev/random")?;
    assert_eq!(source.read(&mut [])?, 0);
    let result = observe(&mut source, mode == "active")?;
    println!(
        "ENTROPY_OBSERVATION mode={mode} reads={} bytes={} unavailable={} max_us={}",
        result.reads, result.bytes, result.unavailable, result.max_us
    );
    Ok(())
}

#[cfg(not(target_os = "nuttx"))]
fn main() {
    panic!("NuttX driver fixture; run host tests with cargo test");
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Source {
        active: bool,
        overrun: bool,
    }
    impl Read for Source {
        fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
            if !self.active {
                return Err(ErrorKind::WouldBlock.into());
            }
            let length = bytes.len().min(32);
            bytes[..length].fill(0x17);
            if self.overrun && bytes.len() > length {
                bytes[length] = 0;
            }
            Ok(length)
        }
    }
    #[test]
    fn active_partial_reads_and_refusal_are_distinct() {
        let mut source = Source {
            active: true,
            overrun: false,
        };
        let result = observe(&mut source, true).unwrap();
        assert_eq!(
            (result.reads, result.bytes, result.unavailable),
            (224, 4320, 0)
        );
        assert!(observe(&mut source, false).is_err());
        source.active = false;
        assert_eq!(observe(&mut source, false).unwrap().unavailable, 224);
        assert!(observe(&mut source, true).is_err());
    }
    #[test]
    fn overwritten_suffix_fails() {
        assert!(observe(
            &mut Source {
                active: true,
                overrun: true
            },
            true
        )
        .is_err());
    }
}
