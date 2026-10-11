//! Packet-producing behavior needs no socket or mock HAL crate.
use camera_api::{Capture, Format, Frame, PixelFormat};
use telemetry_service::{Error, Telemetry, TelemetryService, TelemetryStats};
use transport_api::{DeviceError, PacketSink, Transport};

fn frame() -> Frame<'static> {
    Frame {
        sequence: 7,
        capture: Capture {
            format: Format { width: 2, height: 2, pixels: PixelFormat::Gray8 },
            len: 4,
            timestamp_ms: 50,
        },
        bytes: &[7; 4],
    }
}

// Independent fixture, not generated using the production encoder/checksum.
const EXPECTED: [u8; 28] = [
    1, 0, 2, 0, 2, 0, 0, 0,
    7, 0, 0, 0, 0, 0, 0, 0,
    50, 0, 0, 0, 0, 0, 0, 0,
    0x41, 0x30, 0x92, 0x45,
];

#[test]
fn telemetry_can_publish_to_a_stack_only_callback() {
    let mut observed = [0; 28];
    let mut calls = 0;
    {
        let sink = |packet: &[u8]| {
            observed.copy_from_slice(packet);
            calls += 1;
            Ok(())
        };
        let mut service = TelemetryService::new(sink);
        assert_eq!(service.publish(frame()), Ok(()));
        assert_eq!(service.stats(), TelemetryStats { accepted: 1, errors: 0 });
    }
    assert_eq!(calls, 1);
    assert_eq!(observed, EXPECTED);
}

#[test]
fn rejection_is_reported_without_retrying_or_counting_acceptance() {
    let mut calls = 0;
    {
        let sink = |packet: &[u8]| {
            assert_eq!(packet, EXPECTED);
            calls += 1;
            if calls == 1 { Err(DeviceError::Busy) } else { Ok(()) }
        };
        let mut service = TelemetryService::new(sink);
        assert_eq!(service.publish(frame()), Err(Error::Device(DeviceError::Busy)));
        assert_eq!(service.stats(), TelemetryStats { accepted: 0, errors: 1 });
        // Only the caller, not an output framework, requests a retry.
        assert_eq!(service.publish(frame()), Ok(()));
        assert_eq!(service.stats(), TelemetryStats { accepted: 1, errors: 1 });
    }
    assert_eq!(calls, 2);
}

#[test]
fn legacy_transport_name_is_the_identical_trait() {
    fn send_old(sink: &mut dyn Transport) { sink.send(b"old").unwrap(); }
    fn send_new(sink: &mut dyn PacketSink) { sink.send(b"new").unwrap(); }
    let mut count = 0;
    let mut sink = |_: &[u8]| { count += 1; Ok(()) };
    send_old(&mut sink);
    send_new(&mut sink);
    assert_eq!(count, 2);
}
