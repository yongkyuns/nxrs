//! Actual product-encoder vectors for the existing Python peer decoder.
//! Call outside allocator windows. This does not run TLS, BLE, or an iPhone.
#![forbid(unsafe_code)]

use super::capture::{Capture, MAX_BYTES, Version};
use navigation::observations::Measurement;
use std::fmt::Write;

const UTC: i64 = 1_767_225_600_000;
const SCALE: [f32; 2] = [0.125, 0.25];

struct Expected {
    source: u8,
    sequence: u32,
    time_ms: u64,
    utc_ms: Option<i64>,
    values_bits: Vec<u64>,
    delta: bool,
    counts: bool,
    no_utc: bool,
}

fn imu(source: usize, sequence: u32, time_ms: u64, raw_bits: bool) -> Measurement {
    let (accel_mps2, gyro_rps) = if raw_bits {
        ([-0.0, f32::from_bits(0x7fc0_1234), 0.1], [0.25, -0.5, 1.0])
    } else if source == 0 {
        ([-4096.0, 0.0, 4095.875], [-0.25, 1.0, 8191.75])
    } else {
        // Both IMUs advertise scales, but these values require float fallback.
        ([-0.0, 0.1, 1.0], [0.2, -1.0, 3.0])
    };
    Measurement::Imu(
        source,
        imu::ImuSample {
            sequence: u64::from(sequence),
            timestamp_us: time_ms * 1_000 + 700,
            is_capture_time: true,
            sample_interval_us: 10_000,
            accel_mps2,
            gyro_rps,
        },
    )
}

fn fix(sequence: u32, time_ms: u64, receiver_utc_ms: Option<i64>) -> Measurement {
    Measurement::Gnss(gnss::GnssFix {
        sequence: u64::from(sequence),
        timestamp_us: time_ms * 1_000,
        is_capture_time: false,
        receiver_utc_ms,
        latitude_deg: 45.25,
        longitude_deg: -75.125,
        speed_mps: 2.5,
        course_rad: None,
        quality: None,
    })
}

fn expected(
    measurement: Measurement,
    version: Version,
    anchor: Option<(u64, i64)>,
    delta: bool,
    counts: bool,
) -> Expected {
    let (source, sequence, time_ms, values_bits) = match measurement {
        Measurement::Imu(source, sample) => (
            source as u8 + 1,
            sample.sequence as u32,
            sample.timestamp_us / 1_000,
            sample
                .accel_mps2
                .into_iter()
                .chain(sample.gyro_rps)
                .map(|value| u64::from(value.to_bits()))
                .collect(),
        ),
        Measurement::Gnss(sample) => (
            3,
            sample.sequence as u32,
            sample.timestamp_us / 1_000,
            vec![
                sample.latitude_deg.to_bits(),
                sample.longitude_deg.to_bits(),
                u64::from(sample.speed_mps.to_bits()),
            ],
        ),
    };
    let timed = matches!(version, Version::Timed);
    Expected {
        source,
        sequence,
        time_ms: if timed {
            time_ms
        } else {
            u64::from(time_ms as u32)
        },
        utc_ms: anchor
            .filter(|(since, _)| timed && time_ms >= *since)
            .map(|(since, utc)| utc + (time_ms - since) as i64),
        values_bits,
        delta: delta && !matches!(version, Version::Fixed),
        counts: counts && timed,
        no_utc: timed && anchor.is_some_and(|(since, _)| time_ms < since),
    }
}

fn emit(case: &str, version: Version, frame: &[u8], expected: &[Expected], terminal: bool) {
    let mut frame_hex = String::with_capacity(frame.len() * 2);
    for byte in frame {
        write!(&mut frame_hex, "{byte:02x}").unwrap();
    }
    let mut records = String::from("[");
    for (index, item) in expected.iter().enumerate() {
        if index != 0 {
            records.push(',');
        }
        let utc = item
            .utc_ms
            .map_or_else(|| "null".into(), |value| value.to_string());
        write!(&mut records,
            "{{\"source\":{},\"sequence\":{},\"time_ms\":{},\"utc_ms\":{utc},\"values_bits\":{:?},\"delta\":{},\"counts\":{},\"no_utc\":{}}}",
            item.source, item.sequence, item.time_ms, item.values_bits,
            item.delta, item.counts, item.no_utc).unwrap();
    }
    records.push(']');
    let missing = if terminal && !matches!(version, Version::Fixed) {
        "{\"source\":3,\"sequence\":512}"
    } else {
        "null"
    };
    println!(
        "CAPTURE_PEER {{\"schema\":1,\"case\":\"{case}\",\"version\":{},\"peer_fixture_blob\":\"{}\",\"frame_hex\":\"{frame_hex}\",\"scales_bits\":[[{},{}],[{},{}]],\"expected\":{records},\"terminal\":{terminal},\"missing\":{missing},\"accepted_before_terminal\":{},\"swift_finite_domain\":{}}}",
        version as u8,
        super::blob(include_bytes!("capture_peer.rs")),
        SCALE[0].to_bits(),
        SCALE[1].to_bits(),
        SCALE[0].to_bits(),
        SCALE[1].to_bits(),
        if terminal { 512 } else { 0 },
        case != "raw-bits-v3",
    );
}

fn fixture() -> (Capture, [super::capture::CapturePublisher; 3]) {
    Capture::new([Some(SCALE.map(f32::to_bits)); 2], [Some(100_000); 2], true).unwrap()
}

fn mixed(version: Version) {
    let origin = u64::from(u32::MAX) - 2;
    let anchor = (origin + 3, UTC);
    let (mut capture, mut publishers) = fixture();
    publishers[2].record(fix(0, anchor.0, Some(anchor.1)));
    assert!(capture.enable().unwrap());
    let mut records = Vec::new();
    for (round, (sequence, elapsed)) in [(u32::MAX, 0), (0, 9), (2, 300)].into_iter().enumerate() {
        let measurements = std::array::from_fn::<_, 3, _>(|source| {
            // Each source advances, but cross-source timestamps run backwards.
            let time = origin + elapsed + [2, 0, 1][source];
            if source < 2 {
                imu(source, sequence, time, false)
            } else {
                fix(sequence, time, None)
            }
        });
        // Arrival order differs from the pool's initial round-robin drain.
        for source in (0..3).rev() {
            publishers[source].record(measurements[source]);
        }
        for (source, measurement) in measurements.into_iter().enumerate() {
            records.push(expected(
                measurement,
                version,
                Some(anchor),
                round == 1,
                source == 0,
            ));
        }
    }
    let mut bytes = [0; MAX_BYTES];
    let batch = capture.take(&mut bytes, true, version).unwrap().unwrap();
    assert!(!batch.terminal);
    assert!(capture.take(&mut bytes, true, version).unwrap().is_none());
    // take(None) leaves the prior encoded bytes intact.
    emit(
        &format!("mixed-v{}", version as u8),
        version,
        &bytes[..batch.len],
        &records,
        false,
    );
}

fn unanchored() {
    let version = Version::Timed;
    let (mut capture, mut publishers) = fixture();
    assert!(capture.enable().unwrap());
    let first = imu(0, 1, 1_000, false);
    let second = fix(1, 1_001, None);
    publishers[2].record(second);
    publishers[0].record(first);
    let records = [
        expected(first, version, None, false, true),
        expected(second, version, None, false, false),
    ];
    let mut bytes = [0; MAX_BYTES];
    let batch = capture.take(&mut bytes, true, version).unwrap().unwrap();
    emit(
        "unanchored-v3",
        version,
        &bytes[..batch.len],
        &records,
        false,
    );
}

fn raw_bits() {
    let version = Version::Timed;
    let (mut capture, mut publishers) = fixture();
    assert!(capture.enable().unwrap());
    let measurements = [imu(0, 1, 1_000, true), imu(1, 1, 999, false)];
    for source in (0..2).rev() {
        publishers[source].record(measurements[source]);
    }
    let records = measurements.map(|value| expected(value, version, None, false, false));
    let mut bytes = [0; MAX_BYTES];
    let batch = capture.take(&mut bytes, true, version).unwrap().unwrap();
    emit("raw-bits-v3", version, &bytes[..batch.len], &records, false);
}

fn overflow(version: Version) {
    let (mut capture, mut publishers) = fixture();
    assert!(capture.enable().unwrap());
    for sequence in 0..=512 {
        publishers[2].record(fix(sequence, u64::from(sequence) * 500, None));
    }
    let mut bytes = [0; MAX_BYTES];
    for _ in 0..32 {
        assert!(
            !capture
                .take(&mut bytes, false, version)
                .unwrap()
                .unwrap()
                .terminal
        );
    }
    let terminal = capture.take(&mut bytes, false, version).unwrap().unwrap();
    assert!(terminal.terminal);
    assert_eq!(capture.finish(), 512);
    emit(
        &format!("overflow-v{}", version as u8),
        version,
        &bytes[..terminal.len],
        &[],
        true,
    );
}

pub(crate) fn run() {
    for version in [Version::Fixed, Version::Compact, Version::Timed] {
        mixed(version);
    }
    unanchored();
    raw_bits();
    for version in [Version::Fixed, Version::Compact, Version::Timed] {
        overflow(version);
    }
}
