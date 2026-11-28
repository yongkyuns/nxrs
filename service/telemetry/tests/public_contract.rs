//! A separate consumer uses the public protocol without an application worker.
use navigation::observations::{publication, Observation, Reading};
use std::time::{Duration, Instant};
use telemetry::export::{Version, MAX_BYTES};
use telemetry::{Capture, CaptureBudget, CaptureConfig, ClockAnchor, Snapshot};

#[test]
fn imu_only_consumer_selects_its_budget_and_uses_the_existing_wire_contract() {
    let (mut capture, mut publishers) = Capture::new(CaptureConfig {
        scales: [None; 2],
        imu_rates_millihz: [Some(10_000), None],
        gnss_rate_millihz: 0,
        reserve_export: true,
        budget: CaptureBudget {
            max_requested_bytes: 24 * 1024,
            stall_ms: 500,
            burst_records: 8,
        },
    })
    .unwrap();
    assert!(capture.enable().unwrap());
    let sample = imu::ImuSample {
        sequence: 42,
        timestamp_us: 1_234_000,
        is_capture_time: true,
        sample_interval_us: 100_000,
        accel_mps2: [1.0, 2.0, 3.0],
        gyro_rps: [4.0, 5.0, 6.0],
    };
    let now = Instant::now();
    let (mut observations, [mut reader]) = publication::<1, 1>(Duration::from_secs(1), None);
    let measurement = observations.imu[0]
        .record(&Observation::imu(0, imu::Event::Sample(sample), now))
        .unwrap()
        .unwrap();
    publishers[0].record(measurement);
    let mut bytes = [0; MAX_BYTES];
    let batch = capture
        .take(&mut bytes, true, Version::Fixed)
        .unwrap()
        .unwrap();
    assert!(!batch.terminal);
    assert_eq!(batch.len, 37);
    assert_eq!(
        &bytes[..13],
        &[0xa5, 1, 33, 0, 1, 42, 0, 0, 0, 0xd2, 4, 0, 0]
    );
    assert_eq!(&bytes[13..17], &[0, 0, 0x80, 0x3f]); // accel x = 1.0
    assert_eq!(&bytes[33..37], &[0, 0, 0xc0, 0x40]); // gyro z = 6.0
    capture.finish();

    let mut snapshot = Snapshot::from(reader.snapshot(now));
    assert_eq!(snapshot.imu, [Reading::Live(sample), Reading::Waiting]);
    assert_eq!(snapshot.gnss, Reading::Waiting);
    snapshot.clock = ClockAnchor::new(1_234, 1_000_000);
    let encoded = telemetry::wire::frame(&snapshot).unwrap();
    let json = std::str::from_utf8(encoded.bytes()).unwrap();
    assert!(json.contains("\"utc_ms\":1000000"));
    assert!(json.ends_with("\"gnss\":{\"state\":\"waiting\"}}\n"));
    assert!(!json.contains("lat_deg"));
}

#[test]
fn failed_budget_admission_returns_before_any_acquisition_endpoint_is_created() {
    let error = Capture::new(CaptureConfig {
        scales: [None; 2],
        imu_rates_millihz: [Some(100_000); 2],
        gnss_rate_millihz: 100_000,
        reserve_export: true,
        budget: CaptureBudget {
            max_requested_bytes: 24 * 1024,
            stall_ms: 2_000,
            burst_records: 64,
        },
    })
    .err()
    .unwrap();
    assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
    assert!(error.to_string().contains("664 slots"));
}
