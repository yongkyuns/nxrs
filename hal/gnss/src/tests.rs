use super::*;
use nxrs_ubx_driver::Driver;
use peripheral::gnss::Receiver as Protocol;
use std::{
    io::{Read, Write},
    time::{Duration, Instant},
};
fn deadline() -> Instant {
    Instant::now() + Duration::from_secs(1)
}
use std::{collections::VecDeque, io::ErrorKind, vec::Vec};

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
    SatelliteReadAfterSet,
    WrongReadback,
    Silent,
}
struct Stream {
    input: VecDeque<u8>,
    tx: Vec<u8>,
    rate: u8,
    satellite_rate: u8,
    period_ms: u16,
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
            satellite_rate: 4,
            period_ms: 1_000,
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
            if self.tx[3] == 0x8b && length == 12 {
                assert_eq!(&self.tx[6..18], &[0, 0, 0, 0, 1, 0, 0x21, 0x30, 2, 0, 0x21, 0x30]);
                let mut reply = [1, 0, 0, 0, 1, 0, 0x21, 0x30, 0, 0, 2, 0, 0x21, 0x30, 1, 0];
                reply[8..10].copy_from_slice(&self.period_ms.to_le_bytes());
                self.input.extend(frame(6, 0x8b, &reply));
                self.tx.clear();
                return Ok(count);
            }
            let key: [u8; 4] = self.tx[10..14].try_into().unwrap();
            let satellite = key == [0x16, 0, 0x91, 0x20];
            assert!(satellite || key == [7, 0, 0x91, 0x20]);
            let rate = if satellite {
                &mut self.satellite_rate
            } else {
                &mut self.rate
            };
            match self.tx[3] {
                0x8b => {
                    assert_eq!(&self.tx[6..10], &[0, 0, 0, 0]);
                    let mut value = *rate;
                    if self.changed && self.fault == Fault::WrongReadback {
                        value = value.wrapping_add(1);
                        self.fault = Fault::None;
                    }
                    let mut reply = [1, 0, 0, 0, 0, 0, 0, 0, value];
                    reply[4..8].copy_from_slice(&key);
                    self.input.extend(frame(6, 0x8b, &reply));
                }
                0x8a => {
                    // Only RAM; no BBR/flash saves, protocol or baud changes.
                    assert_eq!(&self.tx[6..10], &[0, 1, 0, 0]);
                    let nak = self.fault == Fault::Nak;
                    if nak {
                        self.fault = Fault::None;
                    } else {
                        *rate = self.tx[14];
                        self.changed = true;
                    }
                    if self.fault == Fault::ReadAfterSet
                        || (satellite && self.fault == Fault::SatelliteReadAfterSet)
                    {
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
impl Receiver<Driver<Stream>> {
    // Collect the finite scripted input through the production event-read path.
    fn fixture_fix(&mut self, now_us: u64) -> Result<GnssFix, DeviceError> {
        let mut latest = Err(DeviceError::Busy);
        while !self.source.io().input.is_empty() {
            self.read_events(now_us, |event| match event {
                Ok(Event::Fix(fix)) => latest = Ok(fix),
                Err(error) => latest = Err(error),
                _ => {}
            })?;
        }
        latest
    }
}
fn receiver_with(payload: &[u8]) -> Receiver<Driver<Stream>> {
    let mut stream = Stream::default();
    stream.input.extend(frame(1, 7, payload));
    Receiver::new(Driver::new(stream))
}

#[test]
fn fixes_include_quality_and_only_recent_satellite_reports() {
    let mut stream = Stream::default();
    // More blocks than fit in the old PVT-sized decoder. Include acquired,
    // unusable, code-locked, carrier-locked and unmeasured signal readings.
    let mut sat = [0; 8 + 12 * 9];
    sat[..4].copy_from_slice(&604_799_750u32.to_le_bytes());
    sat[4] = 1;
    sat[5] = 9;
    for (index, (block, (quality, cn0))) in sat[8..]
        .chunks_exact_mut(12)
        .zip([
            (2, 39),
            (3, 28),
            (4, 31),
            (5, 42),
            (7, 0),
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
        ])
        .enumerate()
    {
        block[0] = if index % 2 == 0 { 0 } else { 2 };
        block[1] = index as u8 + 1;
        block[2] = cn0;
        block[8] = quality;
    }
    stream.input.extend(frame(1, 0x35, &sat));
    let mut receiver = Receiver::new(Driver::new(stream));
    let mut satellite_report = None;
    while !receiver.source.io().input.is_empty() {
        receiver
            .read_events(9_000, |event| match event.unwrap() {
                Event::Satellites(report) => satellite_report = Some(report),
                _ => panic!("expected only the satellite report"),
            })
            .unwrap();
    }
    let report = satellite_report.unwrap();
    assert_eq!((report.visible, report.tracked, report.measured), (9, 3, 4));
    assert_eq!(report.interval_ms, 0);
    assert_eq!(
        report.signals()[1],
        SatelliteSignal {
            constellation: Constellation::Galileo,
            id: 2,
            cn0_db_hz: 28,
        }
    );
    let mut fix = pvt(250);
    fix[23] = 2;
    fix[40..44].copy_from_slice(&2_500u32.to_le_bytes());
    fix[44..48].copy_from_slice(&4_750u32.to_le_bytes());
    fix[68..72].copy_from_slice(&250u32.to_le_bytes());
    fix[76..78].copy_from_slice(&175u16.to_le_bytes());
    receiver.source.io_mut().input.extend(frame(1, 7, &fix));

    let quality = receiver.fixture_fix(10_000).unwrap().quality.unwrap();
    assert_eq!(quality.satellites_used, 2);
    assert_eq!(quality.horizontal_accuracy_m, 2.5);
    assert_eq!(quality.vertical_accuracy_m, 4.75);
    assert_eq!(quality.speed_accuracy_mps, 0.25);
    assert_eq!(quality.position_dop, Some(1.75));
    let satellites = quality.satellites.unwrap();
    assert_eq!((satellites.visible, satellites.tracked), (9, 3));
    assert_eq!(satellites.mean_cn0_db_hz, Some(37));
    assert_eq!(satellites.age_ms, 500); // Receiver week rollover.

    receiver
        .source
        .io_mut()
        .input
        .extend(frame(1, 7, &pvt(3_000)));
    assert!(receiver
        .fixture_fix(20_000)
        .unwrap()
        .quality
        .unwrap()
        .satellites
        .is_none());

    sat[..4].copy_from_slice(&4_000u32.to_le_bytes());
    receiver.source.io_mut().input.extend(frame(1, 0x35, &sat));
    receiver
        .source
        .io_mut()
        .input
        .extend(frame(1, 7, &pvt(3_500)));
    assert!(receiver
        .fixture_fix(30_000)
        .unwrap()
        .quality
        .unwrap()
        .satellites
        .is_none());
    receiver.source.io_mut().input.extend(frame(1, 7, &pvt(4_000)));
    let satellites = receiver.fixture_fix(32_000).unwrap().quality.unwrap().satellites.unwrap();
    assert_eq!(satellites.age_ms, 0);
}

fn report_pulse(receiver: &mut Receiver<Driver<Stream>>, epoch: u32, flags: u8, now: u64) {
    let mut pulse = [0; 16];
    pulse[..4].copy_from_slice(&epoch.to_le_bytes());
    pulse[14] = flags;
    receiver.source.io_mut().input.extend(frame(0x0d, 1, &pulse));
    while !receiver.source.io().input.is_empty() {
        receiver.read_events(now, |_| panic!("pulse is not a fix")).unwrap();
    }
}

#[test]
fn pps_recovers_the_device_time_of_fixes_across_week_rollover() {
    let mut receiver = Receiver::new(Driver::new(Stream::default()));
    report_pulse(&mut receiver, 604_799_000, 0x10, 9_700_000);
    receiver.observe_edge(1, 10_000_000);
    report_pulse(&mut receiver, 0, 0x10, 10_700_000);
    receiver.observe_edge(2, 11_000_000);

    for (epoch, received, expected) in [(0, 11_052_000, 11_000_000), (500, 11_537_000, 11_500_000)] {
        receiver.source.io_mut().input.extend(frame(1, 7, &pvt(epoch)));
        let fix = receiver.fixture_fix(received).unwrap();
        assert_eq!(fix.timestamp_us, expected);
        assert!(fix.is_capture_time);
    }
    receiver.source.io_mut().input.extend(frame(1, 7, &pvt(250)));
    assert!(!receiver.fixture_fix(11_920_000).unwrap().is_capture_time);

    report_pulse(&mut receiver, 1_000, 0x10, 11_700_000);
    receiver.observe_edge(4, 12_000_000); // a lost GPIO edge invalidates the pair
    receiver.source.io_mut().input.extend(frame(1, 7, &pvt(1_000)));
    let fix = receiver.fixture_fix(12_050_000).unwrap();
    assert_eq!(fix.timestamp_us, 12_050_000);
    assert!(!fix.is_capture_time);

    report_pulse(&mut receiver, 2_000, 0x20, 12_700_000);
    receiver.observe_edge(5, 13_000_000);
    receiver.source.io_mut().input.extend(frame(1, 7, &pvt(2_000)));
    assert!(!receiver.fixture_fix(13_050_000).unwrap().is_capture_time);
}

#[test]
fn pps_uses_resolved_utc_with_the_m9_pulse_flags() {
    let mut receiver = Receiver::new(Driver::new(Stream::default()));
    report_pulse(&mut receiver, 0, 0x17, 9_700_000);
    receiver.observe_edge(1, 10_000_000);
    report_pulse(&mut receiver, 1_000, 0x17, 10_700_000);
    receiver.observe_edge(2, 11_000_000);

    let mut payload = pvt(19_400);
    payload[4..6].copy_from_slice(&2024_u16.to_le_bytes());
    payload[6..12].copy_from_slice(&[1, 7, 0, 0, 1, 7]); // Sunday 00:00:01 UTC
    payload[16..20].copy_from_slice(&400_000_000_i32.to_le_bytes());
    receiver.source.io_mut().input.extend(frame(1, 7, &payload));
    let fix = receiver.fixture_fix(11_445_000).unwrap();
    assert_eq!(fix.timestamp_us, 11_400_000);
    assert!(fix.is_capture_time);
}

#[test]
fn fragmented_nav_pvt_normalizes_units_and_never_replays_an_epoch() {
    let mut receiver = receiver_with(&pvt(1000));
    let fix = receiver.fixture_fix(123).unwrap();
    assert_eq!((fix.sequence, fix.timestamp_us), (1, 123));
    assert!(!fix.is_capture_time);
    assert!((fix.latitude_deg - 48.1173).abs() < 1e-7);
    assert!((fix.longitude_deg + 11.5166667).abs() < 1e-7);
    assert!((fix.speed_mps - 5.144).abs() < 1e-6);
    assert!((fix.course_rad.unwrap() - core::f32::consts::FRAC_PI_2).abs() < 1e-6);
    receiver
        .source
        .io_mut()
        .input
        .extend(frame(1, 7, &pvt(1000)));
    assert_eq!(receiver.fixture_fix(124), Err(DeviceError::Busy));
    let mut still = pvt(2000);
    still[60..64].fill(0);
    receiver.source.io_mut().input.extend(frame(1, 7, &still));
    let fix = receiver.fixture_fix(125).unwrap();
    assert_eq!(fix.sequence, 2);
    assert_eq!(fix.course_rad, None);
}
#[test]
fn rejects_unusable_fixes_and_bad_coordinates() {
    for (offset, value) in [(20, 0), (20, 1), (20, 5), (20, 0xff), (21, 0), (78, 1)] {
        let mut payload = pvt(1);
        payload[offset] = value;
        assert!(receiver_with(&payload).fixture_fix(1).is_err());
    }
    // Invalid/unavailable fix fields need not be meaningful coordinates.
    let mut invalid = pvt(2);
    invalid[24..28].copy_from_slice(&i32::MAX.to_le_bytes());
    invalid[28..32].copy_from_slice(&i32::MAX.to_le_bytes());
    invalid[64..68].copy_from_slice(&i32::MAX.to_le_bytes());
    for (offset, value) in [(20, 0), (21, 0), (78, 1)] {
        let mut payload = invalid;
        payload[offset] = value;
        assert_eq!(
            receiver_with(&payload).fixture_fix(1),
            Err(DeviceError::Busy)
        );
    }
    for (offset, value) in [(24, 1_800_000_001i32), (28, -900_000_001), (60, -1)] {
        let mut payload = pvt(1);
        payload[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        assert_eq!(
            receiver_with(&payload).fixture_fix(1),
            Err(DeviceError::InvalidData)
        );
    }
}
#[test]
fn checksum_length_noise_resynchronize_and_each_event_read_is_bounded() {
    let mut receiver = receiver_with(&pvt(1));
    *receiver.source.io_mut().input.back_mut().unwrap() ^= 1;
    assert_eq!(receiver.fixture_fix(1), Err(DeviceError::InvalidData));
    receiver
        .source
        .io_mut()
        .input
        .extend([0xb5, 0x62, 1, 7, 0xff, 0xff]);
    receiver.source.io_mut().input.extend(frame(1, 7, &[0; 91]));
    receiver
        .source
        .io_mut()
        .input
        .extend(b"$GNRMC,ignored*00\r\n");
    receiver.source.io_mut().input.extend(frame(1, 7, &pvt(2)));
    assert_eq!(receiver.fixture_fix(2).unwrap().sequence, 1);
    assert_eq!(receiver.fixture_fix(3), Err(DeviceError::Busy));
    receiver
        .source
        .io_mut()
        .input
        .extend(core::iter::repeat_n(b'x', 1000));
    let before = receiver.source.io().reads;
    receiver
        .read_events(4, |_| panic!("noise produced a fix"))
        .unwrap();
    assert_eq!(receiver.source.io().reads - before, 1);
    assert_eq!(receiver.source.io().input.len(), 989);
}
#[test]
fn ram_configuration_is_acknowledged_read_back_and_restored() {
    for (baud, period_ms, satellite_rate) in [
        (38_400, 40, Some(75)),
        (38_400, 1_000, Some(3)),
        (38_400, 10_000, Some(1)),
        (9_600, 1_000, Some(5)),
        (9_600, 125, None),
    ] {
        let stream = Stream {
            period_ms,
            ..Stream::default()
        };
        let mut receiver = Receiver::new(Driver::new(stream).with_uart_baud(baud));
        receiver.source.configure(deadline()).unwrap();
        assert_eq!(receiver.source.io().rate, 1);
        assert_eq!(receiver.source.io().satellite_rate, satellite_rate.unwrap_or(4));
        receiver.source.restore(deadline()).unwrap();
        assert_eq!(receiver.source.io().rate, 7);
        assert_eq!(receiver.source.io().satellite_rate, 4);
        let commands = if satellite_rate.is_some() { 11 } else { 6 };
        assert_eq!(receiver.source.io().commands, commands);
        receiver.source.restore(deadline()).unwrap();
        assert_eq!(receiver.source.io().commands, commands);
    }
}
#[test]
fn negative_ack_readback_mismatch_and_uncertain_apply_can_be_rolled_back() {
    for fault in [
        Fault::Nak,
        Fault::ReadAfterSet,
        Fault::WrongReadback,
        Fault::SatelliteReadAfterSet,
    ] {
        let mut receiver = Receiver::new(Driver::new(Stream {
            fault,
            ..Stream::default()
        }));
        assert!(receiver.source.configure(deadline()).is_err());
        receiver.source.restore(deadline()).unwrap();
        assert_eq!(receiver.source.io().rate, 7);
        assert_eq!(receiver.source.io().satellite_rate, 4);
    }
    let mut receiver = Receiver::new(Driver::new(Stream {
        fault: Fault::Silent,
        ..Stream::default()
    }));
    assert_eq!(
        receiver
            .source
            .configure(Instant::now() + Duration::from_millis(5)),
        Err(peripheral::gnss::Error::Timeout)
    );
    receiver.source.restore(deadline()).unwrap();
    assert_eq!(receiver.source.io().commands, 1);
}

#[test]
fn failed_restoration_stays_pending_until_it_is_verified() {
    let mut receiver = Receiver::new(Driver::new(Stream::default()));
    receiver.source.configure(deadline()).unwrap();
    receiver.source.io_mut().fault = Fault::Silent;
    assert_eq!(
        receiver
            .source
            .restore(Instant::now() + Duration::from_millis(5)),
        Err(peripheral::gnss::Error::Timeout)
    );
    receiver.source.io_mut().fault = Fault::None;
    receiver.source.restore(deadline()).unwrap();
    assert_eq!(receiver.source.io().rate, 7);
    assert_eq!(receiver.source.io().satellite_rate, 4);
}

#[test]
fn event_reads_deliver_every_fragmented_fix_without_coalescing() {
    let mut receiver = Receiver::new(Driver::new(Stream::default()));
    for epoch in 1..=3 {
        receiver
            .source
            .io_mut()
            .input
            .extend(frame(1, 7, &pvt(epoch)));
    }
    let mut fixes = Vec::new();
    while !receiver.source.io().input.is_empty() {
        receiver
            .read_events(123, |event| match event.unwrap() {
                Event::Fix(fix) => fixes.push(fix),
                _ => panic!("expected a fix"),
            })
            .unwrap();
    }
    assert_eq!(
        fixes.iter().map(|fix| fix.sequence).collect::<Vec<_>>(),
        [1, 2, 3]
    );
    assert!(fixes.iter().all(|fix| fix.timestamp_us == 123));
    receiver
        .read_events(124, |_| panic!("replayed fix"))
        .unwrap();
}

#[test]
fn corrupt_event_frame_is_reported_then_next_valid_fix_is_delivered() {
    let mut receiver = Receiver::new(Driver::new(Stream::default()));
    let mut wire = frame(1, 7, &pvt(1));
    *wire.last_mut().unwrap() ^= 1;
    receiver.source.io_mut().input.extend(wire);
    receiver.source.io_mut().input.extend(frame(1, 7, &pvt(2)));
    let mut events = Vec::new();
    while !receiver.source.io().input.is_empty() {
        receiver.read_events(0, |event| events.push(event)).unwrap();
    }
    assert_eq!(events.len(), 2);
    assert_eq!(events[0], Err(DeviceError::InvalidData));
    assert!(matches!(events[1], Ok(Event::Fix(fix)) if fix.sequence == 1));
}

#[test]
fn no_fix_is_delivered_without_ending_acquisition() {
    let mut receiver = receiver_with(&pvt(1));
    let mut unavailable = pvt(2);
    unavailable[21] = 0;
    receiver
        .source
        .io_mut()
        .input
        .extend(frame(1, 7, &unavailable));
    receiver.source.io_mut().input.extend(frame(1, 7, &pvt(3)));
    let mut events = Vec::new();
    while !receiver.source.io().input.is_empty() {
        receiver
            .read_events(123, |event| events.push(event))
            .unwrap();
    }
    assert_eq!(events.len(), 3);
    assert!(matches!(events[0], Ok(Event::Fix(fix)) if fix.sequence == 1));
    assert_eq!(events[1], Ok(Event::NoFix));
    assert!(matches!(events[2], Ok(Event::Fix(fix)) if fix.sequence == 2));
}
