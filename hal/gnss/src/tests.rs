use super::*;
use std::{collections::VecDeque, vec::Vec};

// Independent wire fixtures use M9 SPG4.04/protocol32.01 offsets, not the
// dependency's packet builders. No receiver hardware or RF is emulated.
fn frame(class: u8, id: u8, payload: &[u8]) -> Vec<u8> {
    let mut out = std::vec![0xb5, 0x62, class, id];
    out.extend((payload.len() as u16).to_le_bytes());
    out.extend(payload);
    let (a, b) = out[2..].iter().fold((0u8, 0u8), |(a, b), x| {
        let a = a.wrapping_add(*x);
        (a, b.wrapping_add(a))
    });
    out.extend([a, b]);
    out
}
fn pvt(epoch: u32) -> [u8; 92] {
    let mut p = [0; 92];
    p[..4].copy_from_slice(&epoch.to_le_bytes());
    p[20] = 3;
    p[21] = 1; // 3D, gnssFixOK; UTC flags deliberately invalid.
    p[24..28].copy_from_slice(&(-115_166_667i32).to_le_bytes());
    p[28..32].copy_from_slice(&481_173_000i32.to_le_bytes());
    p[60..64].copy_from_slice(&5144i32.to_le_bytes());
    p[64..68].copy_from_slice(&9_000_000i32.to_le_bytes());
    p[68..72].copy_from_slice(&100u32.to_le_bytes());
    p[72..76].copy_from_slice(&100_000u32.to_le_bytes());
    p
}
#[derive(Clone, Copy, PartialEq)]
enum Fault {
    None,
    Nak,
    ReadAfterSet,
    WrongReadback,
    Silent,
}
struct Stream {
    input: VecDeque<u8>,
    tx: Vec<u8>,
    rate: u8,
    reads: usize,
    commands: usize,
    fault: Fault,
    read_error: bool,
    changed: bool,
}
impl Default for Stream {
    fn default() -> Self {
        Self {
            input: VecDeque::new(),
            tx: Vec::new(),
            rate: 7,
            reads: 0,
            commands: 0,
            fault: Fault::None,
            read_error: false,
            changed: false,
        }
    }
}
impl Read for Stream {
    fn read(&mut self, target: &mut [u8]) -> std::io::Result<usize> {
        self.reads += 1;
        if core::mem::take(&mut self.read_error) {
            return Err(ErrorKind::Other.into());
        }
        if self.input.is_empty() {
            return Err(ErrorKind::WouldBlock.into());
        }
        let count = target.len().min(11).min(self.input.len());
        for item in &mut target[..count] {
            *item = self.input.pop_front().unwrap();
        }
        Ok(count)
    }
}
impl Write for Stream {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        let count = bytes.len().min(3); // Every command must survive partial writes.
        self.tx.extend(&bytes[..count]);
        if self.tx.len() < 6 {
            return Ok(count);
        }
        let length = u16::from_le_bytes([self.tx[4], self.tx[5]]) as usize;
        if self.tx.len() < length + 8 {
            return Ok(count);
        }
        assert_eq!(&self.tx[..3], &[0xb5, 0x62, 6]);
        assert_eq!(frame(6, self.tx[3], &self.tx[6..6 + length]), self.tx);
        self.commands += 1;
        if self.fault != Fault::Silent {
            match self.tx[3] {
                0x8b => {
                    assert_eq!(&self.tx[6..14], &[0, 0, 0, 0, 7, 0, 0x91, 0x20]);
                    let mut rate = self.rate;
                    if self.changed && self.fault == Fault::WrongReadback {
                        rate = rate.wrapping_add(1);
                        self.fault = Fault::None;
                    }
                    self.input
                        .extend(frame(6, 0x8b, &[1, 0, 0, 0, 7, 0, 0x91, 0x20, rate]));
                }
                0x8a => {
                    // Only RAM; no BBR/flash saves, protocol or baud changes.
                    assert_eq!(&self.tx[6..14], &[0, 1, 0, 0, 7, 0, 0x91, 0x20]);
                    let nak = self.fault == Fault::Nak;
                    if nak {
                        self.fault = Fault::None;
                    } else {
                        self.rate = self.tx[14];
                        self.changed = true;
                    }
                    if self.fault == Fault::ReadAfterSet {
                        self.read_error = true;
                        self.fault = Fault::None;
                    }
                    self.input.extend(frame(5, u8::from(!nak), &[6, 0x8a]));
                }
                _ => panic!("unexpected receiver command"),
            }
        }
        self.tx.clear();
        Ok(count)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn receiver_with(payload: &[u8]) -> Receiver<Stream> {
    let mut stream = Stream::default();
    stream.input.extend(frame(1, 7, payload));
    Receiver::new(stream)
}
#[test]
fn fragmented_nav_pvt_normalizes_units_and_never_replays_an_epoch() {
    let wire = frame(1, 7, &pvt(1000));
    for split in 0..=wire.len() {
        let mut decoder = ubx::Decoder::default();
        let mut produced = 0;
        for chunk in [&wire[..split], &wire[split..]] {
            decoder.consume(chunk, 123, |event| {
                produced += usize::from(matches!(event, ubx::Event::Fix(Ok(_))))
            });
        }
        assert_eq!(produced, 1);
    }
    let mut receiver = receiver_with(&pvt(1000));
    let fix = receiver.fix(123).unwrap();
    assert_eq!((fix.sequence, fix.timestamp_ms), (1, 123));
    assert!((fix.latitude_deg - 48.1173).abs() < 1e-7);
    assert!((fix.longitude_deg + 11.5166667).abs() < 1e-7);
    assert!((fix.speed_mps - 5.144).abs() < 1e-6);
    assert!((fix.course_rad.unwrap() - core::f32::consts::FRAC_PI_2).abs() < 1e-6);
    receiver.io.input.extend(frame(1, 7, &pvt(1000)));
    assert_eq!(receiver.fix(124), Err(DeviceError::Busy));
    let mut still = pvt(2000);
    still[60..64].fill(0);
    receiver.io.input.extend(frame(1, 7, &still));
    let fix = receiver.fix(125).unwrap();
    assert_eq!(fix.sequence, 2);
    assert_eq!(fix.course_rad, None);
    assert!(core::mem::size_of::<ubx::Decoder>() <= 160);
}
#[test]
fn rejects_unusable_fixes_and_bad_coordinates() {
    for (offset, value) in [(20, 0), (20, 1), (20, 5), (20, 0xff), (21, 0), (78, 1)] {
        let mut payload = pvt(1);
        payload[offset] = value;
        assert!(receiver_with(&payload).fix(1).is_err());
    }
    // Invalid/unavailable fix fields need not be meaningful coordinates.
    let mut invalid = pvt(2);
    invalid[24..28].copy_from_slice(&i32::MAX.to_le_bytes());
    invalid[28..32].copy_from_slice(&i32::MAX.to_le_bytes());
    invalid[64..68].copy_from_slice(&i32::MAX.to_le_bytes());
    for (offset, value) in [(20, 0), (21, 0), (78, 1)] {
        let mut payload = invalid;
        payload[offset] = value;
        assert_eq!(receiver_with(&payload).fix(1), Err(DeviceError::Busy));
    }
    for (offset, value) in [(24, 1_800_000_001i32), (28, -900_000_001), (60, -1)] {
        let mut payload = pvt(1);
        payload[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        assert_eq!(
            receiver_with(&payload).fix(1),
            Err(DeviceError::InvalidData)
        );
    }
}
#[test]
fn checksum_length_noise_and_backlog_are_bounded_and_resynchronize() {
    let mut receiver = receiver_with(&pvt(1));
    *receiver.io.input.back_mut().unwrap() ^= 1;
    assert_eq!(receiver.fix(1), Err(DeviceError::InvalidData));
    receiver.io.input.extend([0xb5, 0x62, 1, 7, 0xff, 0xff]);
    receiver.io.input.extend(frame(1, 7, &[0; 91]));
    receiver.io.input.extend(b"$GNRMC,ignored*00\r\n");
    receiver.io.input.extend(frame(1, 7, &pvt(2)));
    assert_eq!(receiver.fix(2).unwrap().sequence, 1);
    assert_eq!(receiver.fix(3), Err(DeviceError::Busy));
    receiver.io.input.extend(core::iter::repeat_n(b'x', 1000));
    let before = receiver.io.reads;
    assert_eq!(receiver.fix(4), Err(DeviceError::Busy));
    assert_eq!(receiver.io.reads - before, 32);
}
#[test]
fn malformed_or_unrequested_configuration_values_are_rejected() {
    for payload in [
        std::vec![1, 0, 0, 0, 7, 0, 0x91, 0x20],    // Truncated U1.
        std::vec![1, 0, 0, 0, 1, 0, 0x74, 0x10, 2], // Reserved bool.
        std::vec![1, 0, 0, 0, 1, 0, 0x21, 0x30, 1], // Truncated U2.
    ] {
        let mut decoder = ubx::Decoder::default();
        let mut accepted = false;
        decoder.consume(&frame(6, 0x8b, &payload), 0, |e| {
            accepted |= matches!(e, ubx::Event::Rate(_));
        });
        assert!(!accepted);
    }
}
#[test]
fn ram_configuration_is_acknowledged_read_back_and_restored() {
    let mut receiver = Receiver::new(Stream::default());
    receiver.configure().unwrap();
    assert_eq!(receiver.io.rate, 1);
    receiver.restore().unwrap();
    assert_eq!(receiver.io.rate, 7);
    assert_eq!(receiver.io.commands, 5); // Initial GET, SET/GET, restore SET/GET.
    receiver.restore().unwrap();
    assert_eq!(receiver.io.commands, 5);
}
#[test]
fn negative_ack_readback_mismatch_and_uncertain_apply_can_be_rolled_back() {
    for fault in [Fault::Nak, Fault::ReadAfterSet, Fault::WrongReadback] {
        let mut receiver = Receiver::new(Stream {
            fault,
            ..Stream::default()
        });
        assert!(receiver.configure().is_err());
        receiver.restore().unwrap();
        assert_eq!(receiver.io.rate, 7);
    }
    let mut receiver = Receiver::new(Stream {
        fault: Fault::Silent,
        ..Stream::default()
    });
    assert_eq!(
        receiver.configure_until(Instant::now() + Duration::from_millis(5)),
        Err(DeviceError::Timeout)
    );
    receiver.restore().unwrap();
    assert_eq!(receiver.io.commands, 1);
}

#[test]
fn failed_restoration_stays_pending_until_it_is_verified() {
    let mut receiver = Receiver::new(Stream::default());
    receiver.configure().unwrap();
    receiver.io.fault = Fault::Silent;
    assert_eq!(
        receiver.restore_until(Instant::now() + Duration::from_millis(5)),
        Err(DeviceError::Timeout)
    );
    assert_eq!(receiver.saved_rate, Some(7));
    receiver.io.fault = Fault::None;
    receiver.restore().unwrap();
    assert_eq!(receiver.io.rate, 7);
    assert_eq!(receiver.saved_rate, None);
}
