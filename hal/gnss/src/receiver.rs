//! Receiver byte-stream ownership and bounded decoding, independent of the OS.
use crate::{DeviceError, Gnss, GnssFix};
use std::io::{ErrorKind, Read};
#[path = "nmea.rs"]
mod nmea;

pub(crate) struct Receiver<R> {
    io: R,
    decoder: nmea::Decoder,
}
impl<R: Read> Receiver<R> {
    pub(crate) fn new(io: R) -> Self {
        Self {
            io,
            decoder: nmea::Decoder::default(),
        }
    }
}
impl<R: Read> Gnss for Receiver<R> {
    fn fix(&mut self, now_ms: u64) -> Result<GnssFix, DeviceError> {
        let mut latest = Err(DeviceError::Busy);
        let mut bytes = [0; 128];
        // Drain toward the most recently received record without unbounded work.
        for _ in 0..32 {
            match self.io.read(&mut bytes) {
                Ok(0) => return Err(DeviceError::Io),
                Ok(count) => {
                    for &byte in &bytes[..count] {
                        if let Some(fix) = self.decoder.push(byte, now_ms) {
                            latest = fix;
                        }
                    }
                }
                Err(error) if error.kind() == ErrorKind::WouldBlock => return latest,
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                Err(_) => return Err(DeviceError::Io),
            }
        }
        // Catch up on the next call instead of presenting an undrained backlog.
        Err(DeviceError::Busy)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Stream {
        bytes: std::collections::VecDeque<u8>,
        reads: usize,
    }
    impl Read for Stream {
        fn read(&mut self, target: &mut [u8]) -> std::io::Result<usize> {
            self.reads += 1;
            if self.bytes.is_empty() {
                return Err(ErrorKind::WouldBlock.into());
            }
            let count = target.len().min(7).min(self.bytes.len());
            for item in &mut target[..count] {
                *item = self.bytes.pop_front().unwrap();
            }
            Ok(count)
        }
    }
    #[test]
    fn short_reads_emit_each_fix_once_and_bound_backlog_work() {
        let body = "GNRMC,120000,A,4807.038,N,01131.000,E,1,90";
        let checksum = body.bytes().fold(0, |a, b| a ^ b);
        let wire = std::format!("${body}*{checksum:02X}\r\n");
        let mut receiver = Receiver::new(Stream {
            bytes: wire.bytes().collect(),
            reads: 0,
        });
        let fix = receiver.fix(123).unwrap();
        assert_eq!((fix.timestamp_ms, fix.sequence), (123, 1));
        assert_eq!(receiver.fix(124), Err(DeviceError::Busy));
        receiver.io.bytes.extend(core::iter::repeat_n(b'x', 1000));
        let before = receiver.io.reads;
        assert_eq!(receiver.fix(125), Err(DeviceError::Busy));
        assert_eq!(receiver.io.reads - before, 32);
    }
}
