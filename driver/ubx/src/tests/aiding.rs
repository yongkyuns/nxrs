use super::{frame, pvt};
use crate::*;
use peripheral::gnss::{
    aiding::{Aid, Outcome, PositionAid, Progress, SubmitError, TimeAid},
    Receiver, TimePulse,
};
use std::collections::{BTreeMap, VecDeque};

struct Stream {
    input: VecDeque<u8>,
    pending: Vec<u8>,
    sent: Vec<Vec<u8>>,
    values: BTreeMap<u32, u16>,
    max_write: usize,
    blocked: bool,
    writes: usize,
}

impl Default for Stream {
    fn default() -> Self {
        Self {
            input: VecDeque::new(),
            pending: Vec::new(),
            sent: Vec::new(),
            values: [
                (0x2091_0007, 7),     // NAV-PVT UART1
                (0x2091_0016, 0),     // NAV-SAT UART1
                (0x2091_017e, 0),     // TIM-TP UART1
                (0x3021_0001, 1_000), // RATE-MEAS
                (0x3021_0002, 1),     // RATE-NAV
            ]
            .into(),
            max_write: usize::MAX,
            blocked: false,
            writes: 0,
        }
    }
}

impl Read for Stream {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        if self.input.is_empty() {
            return Err(ErrorKind::WouldBlock.into());
        }
        let count = bytes.len().min(self.input.len());
        for byte in &mut bytes[..count] {
            *byte = self.input.pop_front().unwrap();
        }
        Ok(count)
    }
}

impl Write for Stream {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.writes += 1;
        if self.blocked {
            return Err(ErrorKind::WouldBlock.into());
        }
        let count = bytes.len().min(self.max_write);
        self.pending.extend(&bytes[..count]);
        if self.pending.len() < 6 {
            return Ok(count);
        }
        let length = u16::from_le_bytes([self.pending[4], self.pending[5]]) as usize;
        if self.pending.len() < length + 8 {
            return Ok(count);
        }
        let packet = std::mem::take(&mut self.pending);
        assert_eq!(packet, frame(packet[2], packet[3], &packet[6..6 + length]));
        if packet[2] == 6 {
            let mut values = &packet[10..6 + length];
            if packet[3] == 0x8b {
                let mut payload = vec![1, 0, 0, 0];
                for key in values.chunks_exact(4) {
                    payload.extend(key);
                    let key = u32::from_le_bytes(key.try_into().unwrap());
                    let value = self.values[&key];
                    payload.push(value as u8);
                    if key >> 28 == 3 {
                        payload.push((value >> 8) as u8);
                    }
                }
                self.input.extend(frame(6, 0x8b, &payload));
            } else {
                assert_eq!(packet[3], 0x8a);
                while !values.is_empty() {
                    let key = u32::from_le_bytes(values[..4].try_into().unwrap());
                    let width = if key >> 28 == 3 { 2 } else { 1 };
                    let value = if width == 2 {
                        u16::from_le_bytes(values[4..6].try_into().unwrap())
                    } else {
                        u16::from(values[4])
                    };
                    self.values.insert(key, value);
                    values = &values[4 + width..];
                }
                self.input.extend(frame(5, 1, &[6, 0x8a]));
            }
        }
        self.sent.push(packet);
        Ok(count)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn time() -> TimeAid {
    TimeAid {
        unix_time_ms: 1_795_869_296_789,
        observed_at_us: 1_000_000,
        uncertainty: Duration::from_millis(2),
        max_age: Duration::from_secs(60),
        drift_ppm: 20,
    }
}

fn position() -> PositionAid {
    PositionAid {
        latitude_deg: 43.32425,
        longitude_deg: -79.84321,
        altitude_m: 123.45,
        accuracy_m: 12.341,
        observed_at_us: 1_000_000,
        max_age: Duration::from_secs(60),
        max_speed_mps: 0.0,
    }
}

fn configured() -> Driver<Stream> {
    let mut driver = Driver::new(Stream::default());
    driver
        .configure(Instant::now() + Duration::from_secs(1))
        .unwrap();
    driver
}

fn acknowledge(driver: &mut Driver<Stream>, kind: u8, accepted: bool, reason: u8) {
    driver.io_mut().input.extend(frame(
        0x13,
        0x60,
        &[
            u8::from(accepted),
            0,
            reason,
            0x40,
            kind,
            0,
            0,
            if kind == 0x10 { 0x80 } else { 0 },
        ],
    ));
}

#[test]
fn initialization_wire_layout_matches_independent_spec_fixtures() {
    let utc = Duration::new(1_795_869_296, 789_123_456);
    assert_eq!(
        &*ubx::initial_time(utc, Duration::new(2, 345_678_901)).unwrap(),
        &[
            0xb5, 0x62, 0x13, 0x40, 0x18, 0x00, 0x10, 0x00, 0x00, 0x80, 0xea, 0x07, 0x0b, 0x1c,
            0x0c, 0x22, 0x38, 0x00, 0x80, 0x11, 0x09, 0x2f, 0x02, 0x00, 0x00, 0x00, 0x35, 0xa4,
            0x9a, 0x14, 0xcb, 0x55,
        ]
    );
    assert_eq!(
        &*ubx::initial_position(43.32425, -79.84321, 123.45, 12.341).unwrap(),
        &[
            0xb5, 0x62, 0x13, 0x40, 0x14, 0x00, 0x01, 0x00, 0x00, 0x00, 0x84, 0xc1, 0xd2, 0x19,
            0x9c, 0xe4, 0x68, 0xd0, 0x39, 0x30, 0x00, 0x00, 0xd4, 0x04, 0x00, 0x00, 0x91, 0x94,
        ]
    );
    for (seconds, expected) in [
        (0, [0xb2, 0x07, 1, 1, 0, 0, 0]),
        (951_782_400, [0xd0, 0x07, 2, 29, 0, 0, 0]),
        (4_107_542_400, [0x34, 0x08, 3, 1, 0, 0, 0]),
        (4_102_444_799, [0x33, 0x08, 12, 31, 23, 59, 59]),
    ] {
        let encoded = ubx::initial_time(Duration::from_secs(seconds), Duration::ZERO).unwrap();
        assert_eq!(&encoded[10..17], &expected);
    }
    assert!(ubx::initial_time(utc, Duration::from_secs(65_536)).is_err());
    assert!(ubx::initial_position(91.0, 0.0, 0.0, 1.0).is_err());
    assert!(ubx::initial_position(0.0, f64::NAN, 0.0, 1.0).is_err());
    assert!(ubx::initial_position(0.0, 0.0, f64::INFINITY, 1.0).is_err());
    assert!(ubx::initial_position(0.0, 0.0, 0.0, 50_000_000.0).is_err());
    let tiny = ubx::initial_position(0.000_000_049, 0.000_000_049, 0.0049, 0.01).unwrap();
    assert_eq!(u32::from_le_bytes(tiny[22..26].try_into().unwrap()), 2);
}

#[test]
fn numeric_utc_keeps_the_full_wire_calendar_and_accuracy_range() {
    // 65536-01-01 is 2_005_949_145_600 seconds after 1970-01-01, counting
    // Gregorian years and leap days independently of the March-based codec.
    let last = ubx::initial_time(
        Duration::new(2_005_949_145_599, 999_999_999),
        Duration::new(65_535, 999_999_999),
    )
    .unwrap();
    assert_eq!(&last[10..17], &[0xff, 0xff, 12, 31, 23, 59, 59]);
    assert_eq!(
        u32::from_le_bytes(last[18..22].try_into().unwrap()),
        999_999_999
    );
    assert_eq!(&last[22..24], &[0xff, 0xff]);
    assert_eq!(
        u32::from_le_bytes(last[26..30].try_into().unwrap()),
        999_999_999
    );
    assert!(ubx::initial_time(Duration::from_secs(2_005_949_145_600), Duration::ZERO).is_err());
    assert!(ubx::initial_time(Duration::MAX, Duration::ZERO).is_err());

    let mut driver = Driver::new(Stream::default());
    assert_eq!(
        driver.begin_aiding(
            Aid::Time(TimeAid {
                unix_time_ms: -1,
                ..time()
            }),
            1_000_000,
            Instant::now() + Duration::from_secs(1),
        ),
        Err(SubmitError::InvalidInput)
    );
    assert_eq!(driver.io().writes, 0);
}

#[test]
fn delivery_rounding_matches_wide_arithmetic_at_fraction_and_overflow_boundaries() {
    let last_second = u64::MAX / 1_000_000;
    let last_nanos = (u64::MAX % 1_000_000 * 1_000) as u32;
    for duration in [
        Duration::ZERO,
        Duration::from_nanos(1),
        Duration::from_nanos(999),
        Duration::from_nanos(1_000),
        Duration::from_nanos(1_001),
        Duration::new(1, 999_999_999),
        Duration::new(last_second, last_nanos - 1),
        Duration::new(last_second, last_nanos),
        Duration::new(last_second, last_nanos + 1),
        Duration::new(last_second + 1, 0),
        Duration::MAX,
    ] {
        let expected = u64::try_from(duration.as_nanos().div_ceil(1_000)).ok();
        assert_eq!(
            crate::aiding::ceil_micros(duration),
            expected,
            "{duration:?}"
        );
    }
}

#[test]
fn blocked_time_is_refreshed_once_then_partial_frame_is_immutable() {
    let mut driver = Driver::new(Stream::default());
    let now = Instant::now();
    driver
        .begin_aiding(Aid::Time(time()), 1_000_000, now + Duration::from_secs(3))
        .unwrap();
    assert_eq!(
        driver.begin_aiding(
            Aid::Position(position()),
            1_000_000,
            now + Duration::from_secs(3)
        ),
        Err(SubmitError::Busy)
    );
    driver.io_mut().blocked = true;
    assert_eq!(
        driver.advance_aiding(now, 1_000_000).unwrap(),
        Progress::Writing
    );
    driver.io_mut().blocked = false;
    driver.io_mut().max_write = 3;
    assert_eq!(
        driver
            .advance_aiding(now + Duration::from_secs(1), 2_000_000)
            .unwrap(),
        Progress::Writing
    );
    let first = driver.io().writes;
    let result = loop {
        let before = driver.io().writes;
        let result = driver
            .advance_aiding(now + Duration::from_secs(2), 3_000_000)
            .unwrap();
        assert_eq!(driver.io().writes - before, 1);
        if result != Progress::Writing {
            break result;
        }
    };
    assert!(driver.io().writes > first);
    assert_eq!(result, Progress::Complete(Ok(Outcome::Unconfirmed)));
    let sent = driver.io().sent.last().unwrap();
    assert_eq!(sent[16], 57); // Prepared at 12:34:57, not admission or last write.
    assert_eq!(
        u32::from_le_bytes(sent[18..22].try_into().unwrap()),
        789_000_000
    );
    assert!(u16::from_le_bytes(sent[22..24].try_into().unwrap()) >= 2);
    assert_eq!(
        driver.advance_aiding(now, 1_000_000).unwrap(),
        Progress::Idle
    );
}

#[test]
fn navigation_pulse_and_satellite_reports_continue_during_partial_aiding_write() {
    let mut driver = configured();
    driver.io_mut().max_write = 3;
    let now = Instant::now();
    driver
        .begin_aiding(
            Aid::Position(position()),
            1_000_000,
            now + Duration::from_secs(1),
        )
        .unwrap();
    assert_eq!(
        driver.advance_aiding(now, 1_000_000).unwrap(),
        Progress::Writing
    );
    driver.io_mut().input.extend(frame(1, 7, &pvt(1_000)));
    let mut pulse = [0; 16];
    pulse[..4].copy_from_slice(&1_000u32.to_le_bytes());
    driver.io_mut().input.extend(frame(0x0d, 1, &pulse));
    acknowledge(&mut driver, 1, true, 0);
    driver
        .io_mut()
        .input
        .extend(frame(1, 0x35, &[0xe8, 3, 0, 0, 1, 0, 0, 0]));
    let (mut fixes, mut pulses, mut satellites) = (0, 0, 0);
    while !driver.io().input.is_empty() {
        driver
            .read_available(|report| match report.unwrap() {
                Report::Fix(_) => fixes += 1,
                Report::Pulse(value) => {
                    assert_eq!(value, Some(TimePulse::Gps(1_000)));
                    pulses += 1;
                }
                Report::Satellites(_) => satellites += 1,
            })
            .unwrap();
    }
    assert_eq!((fixes, pulses, satellites), (1, 1, 1));
    let completion = loop {
        match driver.advance_aiding(now, 1_000_000).unwrap() {
            Progress::Writing => {}
            result => break result,
        }
    };
    assert_eq!(completion, Progress::Complete(Ok(Outcome::Unconfirmed)));
    assert_eq!(
        driver.advance_aiding(now, 1_000_000).unwrap(),
        Progress::Idle
    );
    driver.io_mut().max_write = usize::MAX;
    driver.restore(now + Duration::from_secs(1)).unwrap();
    assert_eq!(driver.io().values[&0x2091_0007], 7);
    assert_eq!(driver.io().values[&0x2091_0016], 0);
}

#[test]
fn late_and_previous_session_acknowledgements_never_confirm_new_seeds() {
    let mut driver = configured();
    for _ in 0..2 {
        let now = Instant::now();
        driver
            .begin_aiding(Aid::Time(time()), 1_000_000, now + Duration::from_secs(1))
            .unwrap();
        driver.io_mut().blocked = true;
        acknowledge(&mut driver, 0x10, true, 0);
        acknowledge(&mut driver, 0x10, false, 5);
        acknowledge(&mut driver, 1, true, 0);
        // Even an unrecognized MGA-ACK payload is an unsupported message;
        // it has no authority over this write or the navigation stream.
        driver.io_mut().input.extend(frame(0x13, 0x60, &[99, 99]));
        driver
            .read_available(|_| panic!("unsupported MGA ACK emitted a report"))
            .unwrap();
        assert_eq!(
            driver.advance_aiding(now, 1_000_000).unwrap(),
            Progress::Writing
        );
        driver.io_mut().blocked = false;
        assert_eq!(
            driver.advance_aiding(now, 1_000_000).unwrap(),
            Progress::Complete(Ok(Outcome::Unconfirmed))
        );
        acknowledge(&mut driver, 0x10, true, 0);
        driver
            .read_available(|_| panic!("late ACK emitted a report"))
            .unwrap();
        assert_eq!(
            driver.advance_aiding(now, 1_000_000).unwrap(),
            Progress::Idle
        );
        driver.restore(now + Duration::from_secs(1)).unwrap();
        driver.configure(now + Duration::from_secs(1)).unwrap();
    }
}

#[test]
fn timeout_and_cancel_before_any_write_leave_transport_usable() {
    let mut driver = Driver::new(Stream::default());
    let now = Instant::now();
    driver
        .begin_aiding(
            Aid::Position(position()),
            1_000_000,
            now + Duration::from_secs(1),
        )
        .unwrap();
    assert_eq!(
        driver
            .advance_aiding(now + Duration::from_secs(1), 2_000_000)
            .unwrap(),
        Progress::Complete(Ok(Outcome::TimedOut))
    );
    driver
        .begin_aiding(
            Aid::Position(position()),
            2_000_000,
            now + Duration::from_secs(2),
        )
        .unwrap();
    driver.cancel_aiding().unwrap();
    assert_eq!(driver.io().writes, 0);
    driver
        .begin_aiding(Aid::Time(time()), 2_000_000, now + Duration::from_secs(2))
        .unwrap();
    assert_eq!(
        driver
            .advance_aiding(now + Duration::from_secs(1), 2_000_000)
            .unwrap(),
        Progress::Complete(Ok(Outcome::Unconfirmed))
    );
}

#[test]
fn freshness_covers_delivery_and_position_uncertainty_covers_motion() {
    let mut driver = Driver::new(Stream::default());
    let now = Instant::now();
    let short = TimeAid {
        max_age: Duration::from_secs(1),
        ..time()
    };
    driver
        .begin_aiding(Aid::Time(short), 1_000_000, now + Duration::from_secs(2))
        .unwrap();
    assert_eq!(
        driver.advance_aiding(now, 1_000_000).unwrap(),
        Progress::Complete(Err(SubmitError::Expired))
    );
    assert_eq!(driver.io().writes, 0);
    let moving = PositionAid {
        accuracy_m: 10.0,
        max_speed_mps: 40.0,
        ..position()
    };
    driver
        .begin_aiding(
            Aid::Position(moving),
            11_000_000,
            now + Duration::from_secs(3),
        )
        .unwrap();
    assert_eq!(
        driver.advance_aiding(now, 11_000_000).unwrap(),
        Progress::Complete(Ok(Outcome::Unconfirmed))
    );
    let sent = driver.io().sent.last().unwrap();
    // 10 m + 40 m/s * (10 s old + 3 s send budget + 28-byte 38400-baud frame).
    // Include another centimetre for coordinate encoding.
    assert_eq!(u32::from_le_bytes(sent[22..26].try_into().unwrap()), 53_031);
}

#[test]
fn partial_frame_timeout_or_cancel_prevents_unsafe_restore_commands() {
    for cancel in [false, true] {
        let mut driver = configured();
        driver.io_mut().max_write = 3;
        let now = Instant::now();
        driver
            .begin_aiding(Aid::Time(time()), 1_000_000, now + Duration::from_secs(1))
            .unwrap();
        assert_eq!(
            driver.advance_aiding(now, 1_000_000).unwrap(),
            Progress::Writing
        );
        let writes = driver.io().writes;
        if cancel {
            assert_eq!(driver.cancel_aiding(), Err(DriverError::Io));
        } else {
            assert_eq!(
                driver.advance_aiding(now + Duration::from_secs(1), 2_000_000),
                Err(DriverError::Io)
            );
        }
        assert_eq!(
            driver.restore(now + Duration::from_secs(2)),
            Err(DriverError::Io)
        );
        assert_eq!(driver.io().writes, writes);
        assert_eq!(
            driver.begin_aiding(
                Aid::Position(position()),
                2_000_000,
                now + Duration::from_secs(2)
            ),
            Err(SubmitError::Io)
        );
    }
}

#[test]
fn queued_earlier_writes_widen_accuracy_and_preserve_delivery_freshness() {
    let mut driver = Driver::new(Stream::default()).with_uart_baud(100);
    let now = Instant::now();
    let deadline = now + Duration::from_secs(1);
    let mut accuracies = Vec::new();
    for _ in 0..2 {
        driver
            .begin_aiding(Aid::Time(time()), 1_000_000, deadline)
            .unwrap();
        assert_eq!(
            driver.advance_aiding(now, 1_000_000).unwrap(),
            Progress::Complete(Ok(Outcome::Unconfirmed))
        );
        let sent = driver.io().sent.last().unwrap();
        accuracies.push(Duration::new(
            u64::from(u16::from_le_bytes(sent[22..24].try_into().unwrap())),
            u32::from_le_bytes(sent[26..30].try_into().unwrap()),
        ));
    }
    // The previous 32-byte 8N1 frame requires 3.2 s at this test baud, and
    // source holdover adds 20 ppm across that additional serialization.
    assert_eq!(
        accuracies[1] - accuracies[0],
        Duration::from_nanos(3_200_064_000)
    );
    let short = TimeAid {
        max_age: Duration::from_secs(5),
        ..time()
    };
    driver
        .begin_aiding(Aid::Time(short), 1_000_000, deadline)
        .unwrap();
    assert_eq!(
        driver.advance_aiding(now, 1_000_000).unwrap(),
        Progress::Complete(Err(SubmitError::Expired))
    );
    assert_eq!(driver.io().sent.len(), 2);
}

#[test]
fn aiding_state_remains_inline_and_bounded() {
    let state = std::mem::size_of::<crate::aiding::Aiding>();
    assert!(state <= 176, "aiding state grew to {state} bytes");
    println!(
        "aiding state: {state} bytes; receiver with zero-sized transport: {} bytes",
        std::mem::size_of::<Driver<()>>()
    );
}
