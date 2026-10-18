//! The SAME assertions execute in native tests, the CLI, Node, and Chromium.
use std::collections::VecDeque;
use nxrs_applications::{AppState, CameraProduct, Error, Monitor, Progress, Recorder};
use camera_service::{CameraService, CameraState, CaptureProgress, Error as CameraError, Frames};
use camera::{Capture, DeviceError, Format, PixelFormat};
use recording_service::{Error as RecordingError, RecordingService};
use telemetry_service::{payload_checksum, Error as TelemetryError, TelemetryService, SUMMARY_BYTES};
use crate::mocks::{CameraAction, MockCamera, MockStorage, MockTransport, FORMAT};

pub type Product = CameraProduct<MockCamera, MockStorage, MockTransport, 16, 2>;

fn assemble(camera: MockCamera, storage: MockStorage, transport: MockTransport) -> Product {
    Product::new(camera, storage, transport).unwrap()
}

fn start(product: &mut Product) {
    product.camera.start(FORMAT).unwrap();
    product.recorder.start(&product.camera).unwrap();
    product.monitor.start(&product.camera).unwrap();
}

fn running(actions: impl IntoIterator<Item = CameraAction>) -> Product {
    let mut product = assemble(MockCamera::new(actions), MockStorage::default(), MockTransport::default());
    start(&mut product);
    product
}

fn finish(product: &mut Product) {
    let report = product.shutdown();
    assert_eq!(report.recorder, Ok(()));
    assert_eq!(report.camera, Ok(()));
    assert!(!product.camera.backend().active);
}

fn packet_sequence(packet: &[u8]) -> u64 {
    u64::from_le_bytes(packet[8..16].try_into().unwrap())
}

fn fanout_uses_one_camera_and_identical_frames() {
    let mut product = running((1..=3).map(CameraAction::Frame));
    for sequence in 1..=3 {
        let now_ms = sequence * 10;
        let report = product.step(now_ms);
        assert_eq!(report.camera, Ok(CaptureProgress::Published(sequence)));
        assert_eq!(report.recorder, Ok(Progress::Processed { sequence, skipped: 0 }));
        assert_eq!(report.monitor, report.recorder);
    }
    assert_eq!(product.camera.backend().starts, 1);
    assert_eq!(product.camera.backend().polls, 3);
    let records = &product.recordings.backend().records;
    let packets = &product.telemetry.backend().packets;
    assert_eq!(records.len(), 3);
    assert_eq!(packets.len(), 3);
    for (index, (record, packet)) in records.iter().zip(packets).enumerate() {
        assert_eq!(record.bytes, vec![index as u8 + 1; 4]);
        assert_eq!(record.capture.timestamp_ms, (index as u64 + 1) * 10);
        assert_eq!(packet.len(), SUMMARY_BYTES);
        assert_eq!(&packet[..8], &[1, 0, 2, 0, 2, 0, 0, 0]);
        assert_eq!(packet_sequence(packet), record.sequence);
        assert_eq!(&packet[16..24], &record.capture.timestamp_ms.to_le_bytes());
        assert_eq!(&packet[24..], &payload_checksum(&record.bytes).to_le_bytes());
    }
    assert_eq!(payload_checksum(b"hello"), 0x4f9f2cab);
    finish(&mut product);
}

fn recorder_stop_restart_does_not_stop_monitor() {
    let mut product = running((1..=3).map(CameraAction::Frame));
    product.step(1);
    product.recorder.stop(&mut product.recordings).unwrap();
    assert_eq!(product.camera.backend().stops, 0);
    let report = product.step(2);
    assert_eq!(report.recorder, Ok(Progress::Idle));
    assert_eq!(product.monitor.stats().processed, 2);
    product.recorder.start(&product.camera).unwrap();
    product.step(3);
    assert_eq!(product.recordings.backend().records.iter().map(|r| r.sequence).collect::<Vec<_>>(), vec![1, 3]);
    assert_eq!(product.recorder.stats().skipped, 0);
    assert_eq!(product.monitor.stats().processed, 3);
    assert_eq!(product.camera.backend().starts, 1);
    finish(&mut product);
}

fn monitor_stop_does_not_stop_recorder() {
    let mut product = running([CameraAction::Frame(1), CameraAction::Frame(2)]);
    product.step(1);
    product.monitor.stop();
    let report = product.step(2);
    assert_eq!(report.monitor, Ok(Progress::Idle));
    assert_eq!(product.recorder.stats().processed, 2);
    assert_eq!(product.camera.backend().stops, 0);
    finish(&mut product);
}

fn applications_compose_without_the_example_product() {
    let mut camera = CameraService::<_, 16, 2>::new(MockCamera::new([CameraAction::Frame(7), CameraAction::Frame(8)])).unwrap();
    let mut storage = RecordingService::new(MockStorage::default());
    let mut recorder = Recorder::default();
    camera.start(FORMAT).unwrap();
    recorder.start(&camera).unwrap();
    camera.poll(1).unwrap();
    recorder.step(&camera, &mut storage).unwrap();
    recorder.stop(&mut storage).unwrap();
    // No dummy telemetry dependency was needed by the recording application.
    let mut telemetry = TelemetryService::new(MockTransport::default());
    let mut monitor = Monitor::default();
    monitor.start(&camera).unwrap();
    camera.poll(2).unwrap();
    monitor.step(&camera, &mut telemetry).unwrap();
    assert_eq!(storage.backend().records[0].bytes, vec![7; 4]);
    assert_eq!(packet_sequence(&telemetry.backend().packets[0]), 2);
    monitor.stop();
    camera.stop().unwrap();
}

fn slow_reader_drops_oldest_without_delaying_peer() {
    let mut product = running((1..=5).map(CameraAction::Frame));
    for time in 1..=5 {
        product.camera.poll(time).unwrap();
        product.monitor.step(&product.camera, &mut product.telemetry).unwrap();
    }
    assert_eq!(product.recorder.step(&product.camera, &mut product.recordings), Ok(Progress::Processed { sequence: 4, skipped: 3 }));
    product.recorder.step(&product.camera, &mut product.recordings).unwrap();
    assert_eq!(product.recorder.stats().processed, 2);
    assert_eq!(product.recorder.stats().skipped, 3);
    assert_eq!(product.monitor.stats().processed, 5);
    assert_eq!(product.monitor.stats().skipped, 0);
    assert_eq!(product.recordings.backend().records.iter().map(|r| r.sequence).collect::<Vec<_>>(), vec![4, 5]);
    finish(&mut product);
}

fn storage_busy_retries_without_duplicate_acceptance() {
    let mut storage = MockStorage::default();
    storage.write_errors.push_back(DeviceError::Busy);
    let mut product = assemble(MockCamera::new([CameraAction::Frame(7)]), storage, MockTransport::default());
    start(&mut product);
    let first = product.step(1);
    assert_eq!(first.recorder, Err(Error::Recording(RecordingError::Device(DeviceError::Busy))));
    assert_eq!(product.monitor.stats().processed, 1);
    assert_eq!(product.recorder.stats().processed, 0);
    let second = product.step(2);
    assert_eq!(second.recorder, Ok(Progress::Processed { sequence: 1, skipped: 0 }));
    assert_eq!(second.monitor, Ok(Progress::Idle));
    product.step(3);
    assert_eq!(product.recordings.backend().records.len(), 1);
    assert_eq!(product.recordings.backend().writes, 2);
    finish(&mut product);
}

fn busy_sink_retry_does_not_poll_camera() {
    let mut storage = MockStorage::default();
    storage.write_errors.push_back(DeviceError::Busy);
    let mut product = assemble(
        MockCamera::new([CameraAction::Frame(7), CameraAction::Pending]),
        storage,
        MockTransport::default(),
    );
    start(&mut product);

    assert_eq!(
        product.poll_camera(1),
        Ok(CaptureProgress::Published(1))
    );
    assert_eq!(product.camera.backend().polls, 1);

    let first = product.process_consumers();
    assert_eq!(first.recorder, Err(Error::Recording(RecordingError::Device(DeviceError::Busy))));
    assert_eq!(
        first.monitor,
        Ok(Progress::Processed {
            sequence: 1,
            skipped: 0
        })
    );
    assert_eq!(product.camera.backend().polls, 1);

    let retry = product.process_consumers();
    assert_eq!(
        retry.recorder,
        Ok(Progress::Processed {
            sequence: 1,
            skipped: 0
        })
    );
    assert_eq!(retry.monitor, Ok(Progress::Idle));
    assert_eq!(product.camera.backend().polls, 1);
    assert_eq!(product.recordings.backend().writes, 2);

    finish(&mut product);
}

fn transport_busy_does_not_repeat_recording() {
    let mut transport = MockTransport::default();
    transport.send_errors.push_back(DeviceError::Busy);
    let mut product = assemble(MockCamera::new([CameraAction::Frame(7)]), MockStorage::default(), transport);
    start(&mut product);
    assert_eq!(product.step(1).monitor, Err(Error::Telemetry(TelemetryError::Device(DeviceError::Busy))));
    assert_eq!(product.step(2).monitor, Ok(Progress::Processed { sequence: 1, skipped: 0 }));
    assert_eq!(product.recordings.backend().writes, 1);
    assert_eq!(product.telemetry.backend().sends, 2);
    assert_eq!(product.telemetry.backend().packets.len(), 1);
    finish(&mut product);
}

fn storage_failure_is_isolated_and_reported() {
    let mut storage = MockStorage::default();
    storage.write_errors.push_back(DeviceError::Io);
    let mut product = assemble(MockCamera::new([CameraAction::Frame(7)]), storage, MockTransport::default());
    start(&mut product);
    let first = product.step(1);
    assert_eq!(first.recorder, Err(Error::Recording(RecordingError::Device(DeviceError::Io))));
    assert!(matches!(first.monitor, Ok(Progress::Processed { .. })));
    assert_eq!(product.recordings.stats().errors, 1);
    assert_eq!(product.recordings.stats().accepted, 0);
    product.step(2);
    assert_eq!(product.recordings.stats().accepted, 1);
    finish(&mut product);
}

fn storage_full_is_bounded_and_does_not_stop_monitor() {
    let storage = MockStorage { capacity: 1, ..MockStorage::default() };
    let mut product = assemble(MockCamera::new((1..=3).map(CameraAction::Frame)), storage, MockTransport::default());
    start(&mut product);
    product.step(1);
    assert_eq!(product.step(2).recorder, Err(Error::Recording(RecordingError::Device(DeviceError::Full))));
    assert_eq!(product.step(3).recorder, Err(Error::Recording(RecordingError::Device(DeviceError::Full))));
    assert_eq!(product.recordings.backend().records.len(), 1);
    assert_eq!(product.monitor.stats().processed, 3);
    finish(&mut product);
}

fn failed_start_is_retryable_and_duplicate_start_rejected() {
    let mut device = MockCamera::new([]);
    device.start_failures = 1;
    let mut camera = CameraService::<_, 16, 2>::new(device).unwrap();
    assert_eq!(camera.start(FORMAT), Err(CameraError::Device(DeviceError::Io)));
    assert_eq!(camera.state(), CameraState::Stopped);
    assert!(!camera.backend().active);
    camera.start(FORMAT).unwrap();
    assert_eq!(camera.start(FORMAT), Err(CameraError::AlreadyRunning));
    assert_eq!(camera.backend().starts, 2);
    camera.stop().unwrap();
}

fn invalid_requested_format_never_starts_device() {
    let mut camera = CameraService::<_, 16, 2>::new(MockCamera::new([])).unwrap();
    for format in [Format { width: 0, ..FORMAT }, Format { width: 100, height: 100, ..FORMAT }] {
        assert_eq!(camera.start(format), Err(CameraError::InvalidFormat));
    }
    assert_eq!(camera.backend().starts, 0);
    assert_eq!(camera.state(), CameraState::Stopped);
    assert!(CameraService::<_, 0, 2>::new(MockCamera::new([])).is_err());
    assert!(CameraService::<_, 16, 0>::new(MockCamera::new([])).is_err());
}

fn negotiated_format_is_used_as_actual_metadata() {
    let actual = Format { width: 1, height: 2, ..FORMAT };
    let mut device = MockCamera::new([CameraAction::Frame(9)]);
    device.negotiated = Some(actual);
    let mut camera = CameraService::<_, 16, 2>::new(device).unwrap();
    assert_eq!(camera.start(FORMAT), Ok(actual));
    assert_eq!(camera.format(), Some(actual));
    camera.poll(10).unwrap();
    let frame = camera.next_after(0).unwrap();
    assert_eq!(frame.capture.format, actual);
    assert_eq!(frame.bytes, &[9, 9]);
    camera.stop().unwrap();
    assert!(Format { pixels: PixelFormat::Jpeg, ..FORMAT }.accepts_len(3));
    assert!(!Format { pixels: PixelFormat::Jpeg, ..FORMAT }.accepts_len(0));
}

fn invalid_negotiation_rolls_back_or_retains_cleanup_owner() {
    for failures in [0, 1] {
        let mut device = MockCamera::new([]);
        device.negotiated = Some(Format { width: 0, ..FORMAT });
        device.stop_failures = failures;
        let mut camera = CameraService::<_, 16, 2>::new(device).unwrap();
        let result = camera.start(FORMAT);
        if failures == 0 {
            assert_eq!(result, Err(CameraError::InvalidFormat));
            assert_eq!(camera.state(), CameraState::Stopped);
        } else {
            assert_eq!(result, Err(CameraError::Device(DeviceError::Busy)));
            assert_eq!(camera.state(), CameraState::StopPending);
            assert!(camera.backend().active);
            assert_eq!(camera.start(FORMAT), Err(CameraError::AlreadyRunning));
            camera.stop().unwrap();
        }
        assert!(!camera.backend().active);
        assert_eq!(camera.backend().polls, 0);
    }
}

fn malformed_capture_cannot_publish_or_corrupt_history() {
    let valid = Capture { format: FORMAT, len: 4, timestamp_ms: 12 };
    let invalid = [
        Capture { len: 17, ..valid },
        Capture { len: 0, ..valid },
        Capture { len: 3, ..valid },
        Capture { format: Format { width: 1, ..FORMAT }, ..valid },
        Capture { timestamp_ms: 13, ..valid },
        Capture { timestamp_ms: 9, ..valid },
    ];
    for bad in invalid {
        let mut camera = CameraService::<_, 16, 2>::new(MockCamera::new([
            CameraAction::Frame(7), CameraAction::Malformed(bad), CameraAction::Frame(9),
        ])).unwrap();
        camera.start(FORMAT).unwrap();
        camera.poll(10).unwrap();
        assert_eq!(camera.poll(12), Err(CameraError::InvalidFrame));
        assert_eq!(camera.latest_sequence(), 1);
        assert_eq!(camera.next_after(0).unwrap().bytes, &[7; 4]);
        assert_eq!(camera.poll(13), Ok(CaptureProgress::Published(2)));
        assert_eq!(camera.next_after(1).unwrap().bytes, &[9; 4]);
        camera.stop().unwrap();
    }
}

fn pending_and_capture_errors_preserve_published_bytes() {
    let mut camera = CameraService::<_, 16, 2>::new(MockCamera::new([
        CameraAction::Frame(5), CameraAction::Pending, CameraAction::Fail(DeviceError::Timeout),
    ])).unwrap();
    camera.start(FORMAT).unwrap();
    camera.poll(1).unwrap();
    assert_eq!(camera.poll(2), Ok(CaptureProgress::Pending));
    assert_eq!(camera.next_after(0).unwrap().bytes, &[5; 4]);
    assert_eq!(camera.poll(3), Err(CameraError::Device(DeviceError::Timeout)));
    assert_eq!(camera.next_after(0).unwrap().bytes, &[5; 4]);
    assert_eq!(camera.latest_sequence(), 1);
    camera.stop().unwrap();
}

fn clock_regression_is_rejected_before_driver_call() {
    let mut product = running([CameraAction::Frame(1), CameraAction::Frame(2)]);
    product.step(20);
    assert_eq!(product.camera.poll(19), Err(CameraError::ClockWentBackwards));
    assert_eq!(product.camera.backend().polls, 1);
    assert_eq!(product.camera.poll(20), Ok(CaptureProgress::Published(2)));
    finish(&mut product);
}

fn camera_stop_failure_is_retryable_and_hides_stale_frames() {
    let mut device = MockCamera::new([CameraAction::Frame(1)]);
    device.stop_failures = 1;
    let mut product = assemble(device, MockStorage::default(), MockTransport::default());
    start(&mut product);
    product.step(1);
    assert_eq!(product.shutdown().camera, Err(Error::Camera(CameraError::Device(DeviceError::Busy))));
    assert_eq!(product.camera.state(), CameraState::StopPending);
    assert!(product.camera.next_after(0).is_none());
    assert_eq!(product.camera.poll(2), Err(CameraError::NotRunning));
    assert_eq!(product.camera.start(FORMAT), Err(CameraError::AlreadyRunning));
    finish(&mut product);
    assert_eq!(product.camera.backend().stops, 2);
    assert_eq!(product.recordings.backend().flushes, 1);
}

fn flush_failure_does_not_skip_other_cleanup() {
    let storage = MockStorage { flush_failures: 1, ..MockStorage::default() };
    let mut product = assemble(MockCamera::new([CameraAction::Frame(1)]), storage, MockTransport::default());
    start(&mut product);
    product.step(1);
    let report = product.shutdown();
    assert_eq!(report.recorder, Err(Error::Recording(RecordingError::Device(DeviceError::Io))));
    assert_eq!(report.camera, Ok(()));
    assert_eq!(product.recorder.state(), AppState::Stopping);
    assert_eq!(product.monitor.state(), AppState::Stopped);
    assert_eq!(product.recorder.step(&product.camera, &mut product.recordings), Ok(Progress::Idle));
    assert_eq!(product.recorder.start(&product.camera), Err(Error::AlreadyRunning));
    finish(&mut product);
    assert_eq!(product.recordings.backend().flushes, 2);
    assert_eq!(product.camera.backend().stops, 1);
}

fn repeated_lifecycles_do_not_replay_old_frames() {
    let mut product = assemble(MockCamera::new((1..=32).map(CameraAction::Frame)), MockStorage::default(), MockTransport::default());
    for time in 1..=32 {
        start(&mut product);
        let report = product.step(time);
        assert_eq!(report.recorder, Ok(Progress::Processed { sequence: time, skipped: 0 }));
        finish(&mut product);
        assert!(product.camera.next_after(0).is_none());
    }
    assert_eq!(product.camera.backend().starts, 32);
    assert_eq!(product.camera.backend().stops, 32);
    assert_eq!(product.recorder.stats().processed, 32);
    assert_eq!(product.monitor.stats().processed, 32);
    assert_eq!(product.recordings.backend().records.len(), 32);
}

fn independent_compositions_have_no_global_state() {
    let mut first = running([CameraAction::Frame(11)]);
    let mut second = running([CameraAction::Frame(22)]);
    first.step(10);
    second.step(99);
    assert_eq!(first.recordings.backend().records[0].bytes, vec![11; 4]);
    assert_eq!(second.recordings.backend().records[0].bytes, vec![22; 4]);
    finish(&mut first);
    assert_eq!(second.camera.state(), CameraState::Running);
    assert_eq!(second.camera.backend().stops, 0);
    assert_eq!(second.camera.next_after(0).unwrap().capture.timestamp_ms, 99);
    finish(&mut second);
}

fn deterministic_interleavings_match_independent_history_model() {
    let actions = (1..=128u64).map(|time| {
        if time % 11 == 0 { CameraAction::Fail(DeviceError::Io) }
        else if time % 7 == 0 { CameraAction::Pending }
        else { CameraAction::Frame(time as u8) }
    }).collect::<Vec<_>>();
    let mut product = running(actions);
    let mut history = VecDeque::new();
    let mut sequence = 0u64;
    let mut cursors = [0u64; 2];
    let mut expected = [Vec::new(), Vec::new()];
    let mut skipped = [0u64; 2];
    for time in 1..=130u64 {
        if time <= 128 {
            let capture = product.camera.poll(time);
            if time % 11 == 0 {
                assert_eq!(capture, Err(CameraError::Device(DeviceError::Io)));
            } else if time % 7 == 0 {
                assert_eq!(capture, Ok(CaptureProgress::Pending));
            } else {
                sequence += 1;
                assert_eq!(capture, Ok(CaptureProgress::Published(sequence)));
                history.push_back((sequence, time));
                if history.len() > 2 { history.pop_front(); }
            }
        }
        for app in 0..2 {
            if time > 128 || time % [5, 3][app] == 0 {
                let next = history.iter().copied().find(|(seq, _)| *seq > cursors[app]);
                let progress = if app == 0 {
                    product.recorder.step(&product.camera, &mut product.recordings)
                } else {
                    product.monitor.step(&product.camera, &mut product.telemetry)
                }.unwrap();
                if let Some((seq, captured_at)) = next {
                    let gap = seq - cursors[app] - 1;
                    assert_eq!(progress, Progress::Processed { sequence: seq, skipped: gap });
                    skipped[app] += gap;
                    cursors[app] = seq;
                    expected[app].push((seq, captured_at));
                } else {
                    assert_eq!(progress, Progress::Idle);
                }
            }
        }
    }
    let records = &product.recordings.backend().records;
    assert_eq!(records.len(), expected[0].len());
    for (record, &(seq, time)) in records.iter().zip(&expected[0]) {
        assert_eq!((record.sequence, record.capture.timestamp_ms), (seq, time));
        assert_eq!(record.bytes, vec![time as u8; 4]);
    }
    let packets = &product.telemetry.backend().packets;
    assert_eq!(packets.len(), expected[1].len());
    for (packet, &(seq, time)) in packets.iter().zip(&expected[1]) {
        assert_eq!(packet_sequence(packet), seq);
        assert_eq!(&packet[16..24], &time.to_le_bytes());
    }
    assert_eq!(product.recorder.stats().skipped, skipped[0]);
    assert_eq!(product.monitor.stats().skipped, skipped[1]);
    finish(&mut product);
}

pub struct Scenario {
    pub name: &'static str,
    pub run: fn(),
}

macro_rules! scenarios {
    ($($name:ident),+ $(,)?) => {
        pub static SCENARIOS: &[Scenario] = &[$(Scenario { name: stringify!($name), run: $name }),+];
        #[cfg(test)]
        mod tests {
            $(#[test] fn $name() { super::$name(); })+
        }
    };
}

scenarios! {
    fanout_uses_one_camera_and_identical_frames,
    recorder_stop_restart_does_not_stop_monitor,
    monitor_stop_does_not_stop_recorder,
    applications_compose_without_the_example_product,
    slow_reader_drops_oldest_without_delaying_peer,
    storage_busy_retries_without_duplicate_acceptance,
    busy_sink_retry_does_not_poll_camera,
    transport_busy_does_not_repeat_recording,
    storage_failure_is_isolated_and_reported,
    storage_full_is_bounded_and_does_not_stop_monitor,
    failed_start_is_retryable_and_duplicate_start_rejected,
    invalid_requested_format_never_starts_device,
    negotiated_format_is_used_as_actual_metadata,
    invalid_negotiation_rolls_back_or_retains_cleanup_owner,
    malformed_capture_cannot_publish_or_corrupt_history,
    pending_and_capture_errors_preserve_published_bytes,
    clock_regression_is_rejected_before_driver_call,
    camera_stop_failure_is_retryable_and_hides_stale_frames,
    flush_failure_does_not_skip_other_cleanup,
    repeated_lifecycles_do_not_replay_old_frames,
    independent_compositions_have_no_global_state,
    deterministic_interleavings_match_independent_history_model,
}
