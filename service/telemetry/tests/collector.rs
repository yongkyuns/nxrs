//! A collector consumer supplies its own byte stream and scheduling owner.
use std::io::{self, Write};
use std::time::{Duration, Instant};
use telemetry::{collector::Reports, Reading, Snapshot};

#[test]
fn report_admission_rejects_invalid_intervals_before_requesting_a_stream() {
    let now = Instant::now();
    for interval in [Duration::ZERO, Duration::MAX] {
        assert_eq!(
            Reports::new(interval, now).err().unwrap().kind(),
            io::ErrorKind::InvalidInput
        );
    }
    assert!(Reports::new(telemetry::collector::MAX_REPORT_INTERVAL, now).is_ok());
}

fn snapshot(sequence: u64) -> Snapshot {
    Snapshot::from(navigation::observations::Snapshot {
        imu: [Reading::Live(imu::ImuSample {
            sequence,
            timestamp_us: sequence * 1_000,
            is_capture_time: true,
            sample_interval_us: 10_000,
            accel_mps2: [0.0; 3],
            gyro_rps: [0.0; 3],
        })],
        gnss: None,
    })
}

struct BrokenStream(Vec<u8>);

impl Write for BrokenStream {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if !self.0.is_empty() {
            return Err(io::ErrorKind::ConnectionReset.into());
        }
        let count = bytes.len().min(7);
        self.0.extend_from_slice(&bytes[..count]);
        Ok(count)
    }

    fn flush(&mut self) -> io::Result<()> {
        panic!("the connection must fail before its first frame is complete")
    }
}

#[test]
fn interrupted_connection_spends_its_interval_and_recovery_skips_missed_periods() {
    let now = Instant::now();
    let interval = Duration::from_secs(60);
    let mut reports = Reports::new(interval, now).unwrap();
    let mut broken = BrokenStream(Vec::new());
    let first = snapshot(10);
    let expected = telemetry::wire::cellular_frame(&first).unwrap();
    assert!(!reports.service(&mut broken, now, || first).unwrap());
    assert_eq!(broken.0, expected.bytes()[..7]);
    assert_eq!(
        reports
            .service(&mut broken, now + Duration::from_millis(10), || {
                panic!("a partial frame must not resample")
            })
            .unwrap_err()
            .kind(),
        io::ErrorKind::ConnectionReset
    );

    reports.reset_stream();
    let mut replacement = Vec::new();
    assert!(!reports
        .service(&mut replacement, now + Duration::from_secs(59), || {
            panic!("reconnection must not renew the reporting allowance")
        })
        .unwrap());
    assert!(replacement.is_empty());

    let resumed = now + 3 * interval;
    let current = snapshot(99);
    let current_frame = telemetry::wire::cellular_frame(&current).unwrap();
    assert!(reports
        .service(&mut replacement, resumed, || current)
        .unwrap());
    assert_eq!(replacement, current_frame.bytes());
    for instant in [resumed, resumed + interval - Duration::from_millis(1)] {
        assert!(!reports
            .service(&mut replacement, instant, || {
                panic!("missed periods must not produce a catch-up burst")
            })
            .unwrap());
    }
    assert!(reports
        .service(&mut replacement, resumed + interval, || current)
        .unwrap());
    assert_eq!(replacement.len(), 2 * current_frame.bytes().len());
    assert_eq!(
        &replacement[current_frame.bytes().len()..],
        current_frame.bytes()
    );
}

struct ShortWrites {
    bytes: Vec<u8>,
    limit: usize,
    blocked_flushes: usize,
}

impl Write for ShortWrites {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let count = self.limit.min(bytes.len());
        self.bytes.extend_from_slice(&bytes[..count]);
        Ok(count)
    }

    fn flush(&mut self) -> io::Result<()> {
        if self.blocked_flushes > 0 {
            self.blocked_flushes -= 1;
            return Err(io::ErrorKind::WouldBlock.into());
        }
        Ok(())
    }
}

#[test]
fn partial_writes_and_flush_backpressure_complete_one_snapshot_before_resampling() {
    let start = Instant::now();
    let mut reports = Reports::new(Duration::from_millis(1), start).unwrap();
    let first = snapshot(5);
    let first_frame = telemetry::wire::cellular_frame(&first).unwrap();
    let mut stream = ShortWrites {
        bytes: Vec::new(),
        limit: 7,
        blocked_flushes: 1,
    };
    assert!(!reports.service(&mut stream, start, || first).unwrap());
    let mut now = start;
    let mut complete = false;
    for _ in 0..first_frame.bytes().len() + 2 {
        now += Duration::from_millis(1);
        complete = reports
            .service(&mut stream, now, || {
                panic!("elapsed intervals must not replace an in-flight snapshot")
            })
            .unwrap();
        if complete {
            break;
        }
    }
    assert!(complete, "the bounded short-write stream did not complete");
    assert_eq!(stream.blocked_flushes, 0);
    assert_eq!(stream.bytes, first_frame.bytes());

    stream.limit = usize::MAX;
    let second = snapshot(50);
    let second_frame = telemetry::wire::cellular_frame(&second).unwrap();
    assert!(reports.service(&mut stream, now, || second).unwrap());
    assert_eq!(
        &stream.bytes[..first_frame.bytes().len()],
        first_frame.bytes()
    );
    assert_eq!(
        &stream.bytes[first_frame.bytes().len()..],
        second_frame.bytes()
    );
    assert!(!reports
        .service(&mut stream, now, || panic!("no duplicate report is due"))
        .unwrap());
}
