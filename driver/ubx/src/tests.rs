use super::*;
use std::{collections::VecDeque, vec::Vec};

// Independent M9 SPG4.04/protocol32.01 wire fixtures; no codec packet builders.
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
    p[21] = 1;
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
            changed: false,
        }
    }
}
impl Read for Stream {
    fn read(&mut self, target: &mut [u8]) -> std::io::Result<usize> {
        self.reads += 1;
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
        let count = bytes.len().min(3);
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
                    let rate = if self.changed && self.fault == Fault::WrongReadback {
                        self.rate.wrapping_add(1)
                    } else {
                        self.rate
                    };
                    self.fault = Fault::None;
                    self.input
                        .extend(frame(6, 0x8b, &[1, 0, 0, 0, 7, 0, 0x91, 0x20, rate]));
                }
                0x8a => {
                    assert_eq!(&self.tx[6..14], &[0, 1, 0, 0, 7, 0, 0x91, 0x20]);
                    let nak = self.fault == Fault::Nak;
                    if !nak {
                        self.rate = self.tx[14];
                        self.changed = true;
                    }
                    if self.fault != Fault::WrongReadback {
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
fn deadline() -> Instant {
    Instant::now() + Duration::from_millis(40)
}

#[test]
fn fragmented_nav_pvt_reports_epoch_coordinates_speed_and_heading_quality() {
    assert!(core::mem::size_of::<ubx::Decoder>() <= 160);
    let wire = frame(1, 7, &pvt(1000));
    for split in 0..=wire.len() {
        let mut decoder = ubx::Decoder::default();
        let mut reports = Vec::new();
        decoder.consume(&wire[..split], |e| {
            if let ubx::Event::NavPvt(r) = e {
                reports.push(r)
            }
        });
        decoder.consume(&wire[split..], |e| {
            if let ubx::Event::NavPvt(r) = e {
                reports.push(r)
            }
        });
        assert_eq!(reports.len(), 1);
        let report = reports[0].unwrap();
        assert_eq!(report.itow_ms, 1000);
        assert!((report.latitude_deg - 48.1173).abs() < 1e-7);
        assert!((report.longitude_deg + 11.5166667).abs() < 1e-7);
        assert!((report.speed_mps - 5.144).abs() < 1e-6);
        assert!((report.heading_rad.unwrap() - core::f32::consts::FRAC_PI_2).abs() < 1e-6);
        assert!(report.speed_accuracy_mps > 0.0 && report.heading_accuracy_rad > 0.0);
    }
    let mut still = pvt(2000);
    still[60..64].fill(0);
    let mut decoder = ubx::Decoder::default();
    let mut reports = Vec::new();
    decoder.consume(&frame(1, 7, &still), |e| {
        if let ubx::Event::NavPvt(r) = e {
            reports.push(r)
        }
    });
    assert_eq!(reports[0].unwrap().heading_rad, None);
}

#[test]
fn invalid_fix_values_and_bad_coordinates_are_rejected() {
    for (offset, value) in [(20, 0), (20, 1), (20, 5), (20, 0xff), (21, 0), (78, 1)] {
        let mut payload = pvt(1);
        payload[offset] = value;
        let mut decoder = ubx::Decoder::default();
        let mut reports = Vec::new();
        decoder.consume(&frame(1, 7, &payload), |e| {
            if let ubx::Event::NavPvt(r) = e {
                reports.push(r)
            }
        });
        assert_eq!(reports, [Err(DriverError::NoFix)]);
    }
    for (offset, value) in [(24, 1_800_000_001i32), (28, 900_000_001), (64, 36_000_001)] {
        let mut payload = pvt(1);
        payload[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        let mut decoder = ubx::Decoder::default();
        let mut reports = Vec::new();
        decoder.consume(&frame(1, 7, &payload), |e| {
            if let ubx::Event::NavPvt(r) = e {
                reports.push(r)
            }
        });
        assert_eq!(reports, [Err(DriverError::InvalidData)]);
    }
}

#[test]
fn checksum_length_noise_resynchronize_and_poll_reads_once() {
    let mut stream = Stream::default();
    let mut bad = frame(1, 7, &pvt(1));
    *bad.last_mut().unwrap() ^= 1;
    stream.input.extend(bad);
    stream.input.extend([0xb5, 0x62, 1, 7, 0xff, 0xff]);
    stream.input.extend(frame(1, 7, &[0; 91]));
    stream.input.extend(b"$GNRMC,ignored*00\r\n");
    stream.input.extend(frame(1, 7, &pvt(2)));
    let mut driver = Driver::new(stream);
    let mut events = Vec::new();
    while !driver.io().input.is_empty() {
        driver.poll(|event| events.push(event)).unwrap();
    }
    assert_eq!(
        events
            .iter()
            .filter(|e| **e == Err(DriverError::InvalidData))
            .count(),
        3
    );
    assert_eq!(events.iter().filter(|e| e.is_ok()).count(), 1);
    driver
        .io_mut()
        .input
        .extend(std::iter::repeat_n(b'x', 1000));
    let before = driver.io().reads;
    driver.poll(|_| panic!("noise produced a report")).unwrap();
    assert_eq!(driver.io().reads - before, 1);
    assert_eq!(driver.io().input.len(), 989);
}

#[test]
fn ram_rate_query_set_ack_and_readback_use_partial_io() {
    let mut driver = Driver::new(Stream::default());
    assert_eq!(driver.query_nav_pvt_uart1_rate(deadline()).unwrap(), 7);
    driver
        .set_and_verify_nav_pvt_uart1_rate(1, deadline())
        .unwrap();
    assert_eq!(driver.io().rate, 1);
    assert_eq!(driver.io().commands, 3);
}

#[test]
fn negative_ack_mismatch_and_silent_transactions_fail_by_deadline() {
    let mut driver = Driver::new(Stream {
        fault: Fault::Nak,
        ..Stream::default()
    });
    assert_eq!(
        driver.set_nav_pvt_uart1_rate(1, deadline()),
        Err(DriverError::InvalidData)
    );
    let mut driver = Driver::new(Stream {
        fault: Fault::WrongReadback,
        ..Stream::default()
    });
    assert_eq!(
        driver.set_and_verify_nav_pvt_uart1_rate(1, deadline()),
        Err(DriverError::InvalidData)
    );
    let mut driver = Driver::new(Stream {
        fault: Fault::Silent,
        ..Stream::default()
    });
    let before = Instant::now();
    assert_eq!(
        driver.query_nav_pvt_uart1_rate(before + Duration::from_millis(5)),
        Err(DriverError::Timeout)
    );
    assert!(before.elapsed() < Duration::from_millis(100));
}

#[test]
fn malformed_or_unrequested_configuration_values_are_not_accepted() {
    for payload in [
        std::vec![1, 0, 0, 0, 7, 0, 0x91, 0x20],
        std::vec![1, 0, 0, 0, 1, 0, 0x74, 0x10, 2],
        std::vec![1, 0, 0, 0, 1, 0, 0x21, 0x30, 1],
    ] {
        let mut decoder = ubx::Decoder::default();
        let mut accepted = false;
        decoder.consume(&frame(6, 0x8b, &payload), |e| {
            accepted |= matches!(e, ubx::Event::Rate(_))
        });
        assert!(!accepted);
    }
}

#[test]
fn decoder_reset_discards_a_partial_frame() {
    let wire = frame(1, 7, &pvt(1));
    let mut driver = Driver::new(Stream::default());
    driver.decoder.consume(&wire[..20], |_| {});
    driver.reset_decoder();
    driver.io_mut().input.extend(&wire[20..]);
    let mut reports = Vec::new();
    while !driver.io().input.is_empty() {
        driver.poll(|event| reports.push(event)).unwrap();
    }
    assert!(reports.is_empty());
}

#[test]
fn duplicate_device_epochs_are_passed_to_the_hal_for_deduplication() {
    let wire = frame(1, 7, &pvt(42));
    let mut stream = Stream::default();
    stream.input.extend(&wire);
    stream.input.extend(&wire);
    let mut driver = Driver::new(stream);
    let mut reports = Vec::new();
    while !driver.io().input.is_empty() {
        driver
            .poll(|event| {
                if let Ok(report) = event {
                    reports.push(report.itow_ms);
                }
            })
            .unwrap();
    }
    assert_eq!(reports, [42, 42]);
}
