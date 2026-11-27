//! A separate consumer uses the public protocol without an application worker.
use navigation::observations::{Measurement, Reading};
use std::time::Duration;
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
    publishers[0].record(Measurement::Imu(0, sample));
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

    let mut snapshot = Snapshot::from(navigation::observations::Snapshot {
        imu: [Reading::Live(sample), Reading::Waiting],
        gnss: None,
    });
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

fn connection_config() -> telemetry::Config {
    let service = ble::peripheral::Service {
        uuid: ble::Uuid([1; 16]),
        ingress: ble::Uuid([2; 16]),
        egress: ble::Uuid([3; 16]),
    };
    telemetry::Config {
        ble: telemetry::BleServices {
            pairing: service,
            telemetry: service,
        },
        pair_on_start: false,
        cellular: None,
        tile_relay: None,
        names: telemetry::Names::default(),
    }
}

#[test]
fn invalid_report_intervals_preserve_the_callers_endpoint_without_starting_io() {
    let (_publishers, [readings]) =
        navigation::observations::publication::<2, 1>(Duration::from_secs(1), None);
    let (capture, _capture_publishers) = Capture::new(CaptureConfig {
        scales: [None; 2],
        imu_rates_millihz: [None; 2],
        gnss_rate_millihz: 0,
        reserve_export: false,
        budget: CaptureBudget {
            max_requested_bytes: 0,
            stall_ms: 0,
            burst_records: 0,
        },
    })
    .unwrap();
    let mut pending = Some(telemetry::Telemetry {
        readings,
        capture,
        interval: Duration::ZERO,
    });
    for interval in [Duration::ZERO, Duration::MAX] {
        pending.as_mut().unwrap().interval = interval;
        let error = telemetry::Communications::start(connection_config(), &mut pending, || {})
            .err()
            .unwrap();
        assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
        assert!(pending.is_some());
    }

    pending.as_mut().unwrap().interval = Duration::from_millis(100);
    for interval in [Duration::ZERO, Duration::MAX] {
        let mut config = connection_config();
        config.cellular = Some(telemetry::CellularConfig {
            apn: "fixture".into(),
            collector: Some(telemetry::Collector {
                endpoint: telemetry::Endpoint::parse("127.0.0.1:1", &"01".repeat(32)).unwrap(),
                interval,
            }),
        });
        let error = telemetry::Communications::start(config, &mut pending, || {})
            .err()
            .unwrap();
        assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
        assert!(pending.is_some());
    }
}
