use navigation_state::observations::{publication, Observation};
use reports::{collector::Reports, Reading, Snapshot};
use std::time::{Duration, Instant};

fn main() {
    let now = Instant::now();
    let (mut sources, [mut reader]) = publication::<1, 1>(Duration::from_secs(1), None);
    assert!(sources.gnss.is_none());
    let sample = inertial::ImuSample {
        sequence: 7,
        timestamp_us: 42_000,
        is_capture_time: true,
        sample_interval_us: 10_000,
        accel_mps2: [1.0, 2.0, 3.0],
        gyro_rps: [4.0, 5.0, 6.0],
    };
    assert!(sources.imu[0]
        .record(&Observation::imu(0, inertial::Event::Sample(sample), now))
        .unwrap()
        .is_some());
    let snapshot = Snapshot::from(reader.snapshot(now));
    assert_eq!(snapshot.imu, [Reading::Live(sample), Reading::Waiting]);
    assert_eq!(snapshot.gnss, Reading::Waiting);
    let frame = reports::wire::frame(&snapshot).unwrap();
    assert!(std::str::from_utf8(frame.bytes())
        .unwrap()
        .contains("\"seq\":7"));

    let mut collector = Reports::new(Duration::from_secs(60), now).unwrap();
    let mut stream = Vec::new();
    assert!(collector.service(&mut stream, now, || snapshot).unwrap());
    let summary = std::str::from_utf8(&stream).unwrap();
    assert!(summary.contains("\"imu\":[\"live\",\"waiting\"]"));
    assert!(summary.contains("\"imu1_received_ms\":42"));
    assert!(summary.ends_with("\"gnss\":{\"state\":\"waiting\"}}\n"));
    assert!(!collector
        .service(&mut stream, now, || panic!("no second report is due"))
        .unwrap());
    println!("telemetry consumer: one IMU and a caller-owned collector stream");
}
