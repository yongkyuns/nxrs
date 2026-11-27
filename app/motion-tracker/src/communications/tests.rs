mod capture;
use super::*;
use navigation::observations::{
    publication, Observation, ObservationPublishers, ObservationReader, Reading,
};
use std::sync::mpsc;
use telemetry::{export, CapturePublisher};

#[test]
fn invalid_intervals_preserve_the_callers_endpoint_before_starting_io() {
    let (_, _, mut pending, _publishers) = telemetry();
    for interval in [Duration::ZERO, Duration::MAX] {
        pending.as_mut().unwrap().interval = interval;
        let error = Communications::start(test_config(), &mut pending, || {})
            .err()
            .unwrap();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert!(pending.is_some());
    }

    pending.as_mut().unwrap().interval = Duration::from_millis(100);
    for interval in [Duration::ZERO, Duration::MAX] {
        let mut config = test_config();
        config.cellular = Some(CellularConfig {
            apn: "fixture".into(),
            collector: Some(Collector {
                endpoint: Endpoint::parse("127.0.0.1:1", &"01".repeat(32)).unwrap(),
                interval,
            }),
        });
        let error = Communications::start(config, &mut pending, || {})
            .err()
            .unwrap();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert!(pending.is_some());
    }
}

#[test]
fn tracker_summary_policy_limits_fast_samples_and_spends_tile_advertisements() {
    let now = Instant::now();
    let mut summaries = Summaries::new(Duration::from_millis(100), now);
    assert!(summaries.due(now, false));
    summaries.accepted(now);
    for step in 1..10 {
        assert!(!summaries.due(now + Duration::from_millis(step * 100), false));
    }

    let map_request = now + Duration::from_millis(250);
    assert!(summaries.due(map_request, true));
    summaries.accepted(map_request);
    assert!(!summaries.due(now + Duration::from_secs(1), false));
    let next = map_request + Duration::from_secs(1);
    assert!(summaries.due(next, false));
    // A busy stream does not call accepted: the due report remains available,
    // without accumulating reports for elapsed intervals.
    assert!(summaries.due(next + Duration::from_millis(15), false));
    summaries.accepted(next + Duration::from_secs(5));
    assert!(!summaries.due(next + Duration::from_millis(5_500), false));

    let mut slower = Summaries::new(Duration::from_secs(3), now);
    slower.accepted(now);
    assert!(!slower.due(now + Duration::from_secs(2), false));
    assert!(slower.due(now + Duration::from_secs(3), false));
    slower.sensor_failed();
    assert!(!slower.due(now + Duration::from_millis(900), false));
    assert!(slower.due(now + Duration::from_secs(1), false));
    slower.accepted(now + Duration::from_secs(1));
    assert!(!slower.due(now + Duration::from_secs(3), false));
}

fn telemetry() -> (
    ObservationPublishers<2, 2>,
    ObservationReader<2>,
    Option<Telemetry>,
    [CapturePublisher; 3],
) {
    let (publishers, [display, readings]) =
        publication::<2, 2>(Duration::from_secs(1), Some(Duration::from_secs(3)));
    let (capture, capture_publishers) =
        Capture::new(capture([None; 2], [Some(100_000); 2], true)).unwrap();
    (
        publishers,
        display,
        Some(Telemetry {
            readings,
            capture,
            interval: Duration::from_millis(500),
        }),
        capture_publishers,
    )
}

fn test_config() -> Config {
    let service = ble::peripheral::Service {
        uuid: ble::Uuid([0; 16]),
        ingress: ble::Uuid([1; 16]),
        egress: ble::Uuid([2; 16]),
    };
    Config {
        ble: BleServices {
            pairing: service,
            telemetry: service,
        },
        pair_on_start: false,
        cellular: None,
        tile_relay: None,
        names: Names::default(),
    }
}

fn sample(sequence: u64) -> imu::ImuSample {
    imu::ImuSample {
        sequence,
        timestamp_us: sequence * 10_000,
        is_capture_time: false,
        sample_interval_us: 10_000,
        accel_mps2: [0.0, 0.0, 9.8],
        gyro_rps: [0.0; 3],
    }
}

#[test]
fn failed_spawn_preserves_the_reader_for_late_start_and_joined_reuse() {
    let (publishers, mut display, mut pending, _capture_publishers) = telemetry();
    let [mut publisher, _] = publishers.imu;
    let now = Instant::now();
    publisher
        .record(&Observation::imu(0, imu::Event::Sample(sample(1)), now))
        .unwrap();
    let error = Communications::start_owner(&mut pending, |_| {
        Err(io::Error::other("thread creation failed"))
    })
    .err()
    .unwrap();
    assert_eq!(error.to_string(), "thread creation failed");
    assert_eq!(
        pending.as_mut().unwrap().snapshot(now).imu[0],
        Reading::Live(sample(1))
    );
    assert_eq!(display.snapshot(now).imu[0], Reading::Live(sample(1)));

    for sequence in 2..=3 {
        publisher
            .record(&Observation::imu(
                0,
                imu::Event::Sample(sample(sequence)),
                now,
            ))
            .unwrap();
        // Run the real owner on the normal host test stack. The product's
        // fixed stack requires optimized target qualification separately.
        let mut links = Communications::start_owner(&mut pending, |incoming| {
            thread::Builder::new().spawn(move || {
                let mut owner = TelemetryOwner::take(&incoming);
                run(
                    &incoming,
                    test_config(),
                    owner.telemetry.as_mut().unwrap(),
                    &mut || {},
                )
            })
        })
        .unwrap();
        assert!(pending.is_none());
        assert!(links.take_telemetry().is_none());
        links.stop().unwrap();
        links.stop().unwrap();
        pending = links.take_telemetry();
        assert!(links.take_telemetry().is_none());
        assert_eq!(
            pending.as_mut().unwrap().snapshot(now).imu[0],
            Reading::Live(sample(sequence))
        );
    }
}

#[test]
fn paused_capture_survives_failed_spawn_stop_and_joined_owner_reuse() {
    use self::capture::{imu, pause_record};
    let (_, _, mut pending, [primary, mut secondary, _gnss]) = telemetry();
    pending.as_mut().unwrap().capture.enable().unwrap();
    let paused = pause_record(primary, imu(0, 10, 100));
    pending.as_mut().unwrap().capture.finish();
    assert!(pending.as_ref().unwrap().capture.retirement_pending());
    assert!(Communications::start_owner(&mut pending, |_| {
        Err(io::Error::other("thread creation failed"))
    })
    .is_err());
    assert!(!pending.as_mut().unwrap().capture.enable().unwrap());

    // Stop/join must return the owner without waiting for that acquisition
    // task. The actual worker still registers its destination and runs serve.
    let (started, attached) = mpsc::channel();
    let mut links = Communications::start_owner(&mut pending, move |incoming| {
        thread::Builder::new().spawn(move || {
            let mut owner = TelemetryOwner::take(&incoming);
            started.send(()).unwrap();
            run(
                &incoming,
                test_config(),
                owner.telemetry.as_mut().unwrap(),
                &mut || {},
            )
        })
    })
    .unwrap();
    attached.recv_timeout(Duration::from_secs(2)).unwrap();
    links.stop().unwrap();
    pending = links.take_telemetry();
    assert!(pending.as_ref().unwrap().capture.retirement_pending());
    assert!(!pending.as_mut().unwrap().capture.enable().unwrap());

    // A later real owner services pending cleanup even without a radio.
    // A deliberate initial view change lets the test observe its first turn.
    let (serviced, turns) = mpsc::channel();
    let mut links = Communications::start_owner(&mut pending, move |incoming| {
        incoming.lock().unwrap().view.link = BleStatus::Connected;
        thread::Builder::new().spawn(move || {
            let mut owner = TelemetryOwner::take(&incoming);
            run(
                &incoming,
                test_config(),
                owner.telemetry.as_mut().unwrap(),
                &mut || {
                    let _ = serviced.send(());
                },
            )
        })
    })
    .unwrap();
    turns.recv_timeout(Duration::from_secs(2)).unwrap();
    links.shared.lock().unwrap().view.link = BleStatus::Connected;
    let _primary = paused.release();
    turns.recv_timeout(Duration::from_secs(2)).unwrap();
    links.stop().unwrap();
    let mut returned = links.take_telemetry().unwrap();
    assert!(!returned.capture.retirement_pending());
    assert!(returned.capture.enable().unwrap());
    secondary.record(imu(1, 20, 200));
    let mut bytes = [0; export::MAX_BYTES];
    let batch = returned
        .capture
        .take(&mut bytes, true, export::Version::Timed)
        .unwrap()
        .unwrap();
    // One timed float record from the new epoch, with the second IMU's tag.
    assert_eq!(batch.len, 49);
    assert_eq!(bytes[1], 3);
    assert_eq!(bytes[12] & 0x1f, 2);
    assert_eq!(u32::from_le_bytes(bytes[13..17].try_into().unwrap()), 20);
}

#[test]
fn the_running_reader_does_not_need_the_communications_control_mutex() {
    let (publishers, _, mut pending, _capture_publishers) = telemetry();
    let [mut publisher, _] = publishers.imu;
    let now = Instant::now();
    let (started, attached) = mpsc::channel();
    let (go, read) = mpsc::channel();
    let (sampled, observation) = mpsc::channel();
    let mut links = Communications::start_owner(&mut pending, move |incoming| {
        thread::Builder::new().spawn(move || {
            let mut owner = TelemetryOwner::take(&incoming);
            started.send(()).unwrap();
            read.recv().unwrap();
            sampled
                .send(owner.telemetry.as_mut().unwrap().snapshot(now).imu[0])
                .unwrap();
            Ok(())
        })
    })
    .unwrap();
    attached.recv_timeout(Duration::from_secs(2)).unwrap();
    let control = links.shared.lock().unwrap();
    publisher
        .record(&Observation::imu(0, imu::Event::Sample(sample(9)), now))
        .unwrap();
    go.send(()).unwrap();
    assert_eq!(
        observation.recv_timeout(Duration::from_secs(2)).unwrap(),
        Reading::Live(sample(9))
    );
    drop(control);
    links.stop().unwrap();
    assert!(links.take_telemetry().is_some());
}

#[test]
fn error_and_unwinding_workers_return_the_reader_after_join() {
    for panic in [false, true] {
        let (_, _, mut pending, _capture_publishers) = telemetry();
        let mut links = Communications::start_owner(&mut pending, move |incoming| {
            thread::Builder::new().spawn(move || {
                let _owner = TelemetryOwner::take(&incoming);
                if panic {
                    let _control = incoming.lock().unwrap();
                    panic!("fixture worker failure");
                }
                Err(io::Error::other("fixture initialization failure"))
            })
        })
        .unwrap();
        assert!(links.stop().is_err());
        let mut returned = links.take_telemetry().unwrap();
        assert_eq!(returned.snapshot(Instant::now()).imu, [Reading::Waiting; 2]);
        assert!(links.take_telemetry().is_none());
    }
}

#[test]
fn joined_owner_controls_are_quiescent_and_state_is_unavailable() {
    let (_, _, mut pending, _publishers) = telemetry();
    let mut links = Communications::start_owner(&mut pending, |incoming| {
        thread::Builder::new().spawn(move || {
            let _owner = TelemetryOwner::take(&incoming);
            Ok(())
        })
    })
    .unwrap();
    links.stop().unwrap();
    links.pair(Pairing::Start);
    links.pair(Pairing::Cancel);
    assert_eq!(links.view(BleView::default()).link, BleStatus::Unavailable);
    assert!(links
        .map(Some(Key {
            zoom: maps::Zoom::default(),
            x: 1,
            y: 2,
        }))
        .is_none());
    assert!(links.shared.lock().unwrap().pairing.is_none());
    assert!(links.shared.lock().unwrap().tile_request.is_none());
    assert!(links.take_telemetry().is_some());
}
