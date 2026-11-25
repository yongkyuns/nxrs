//! Transport-independent snapshots.
use super::Key;
use crate::{
    text::Text,
    tracker::{reading_status, Reading, Snapshot},
};
use std::fmt::{self, Write};
use std::io;

fn utc_json(out: &mut dyn Write, utc_ms: Option<i64>) -> fmt::Result {
    match utc_ms {
        Some(utc_ms) => write!(out, ",\"utc_ms\":{utc_ms}"),
        None => out.write_str(",\"utc_ms\":null"),
    }
}

fn imu_json(
    out: &mut impl Write,
    reading: Reading<imu::ImuSample>,
    utc_ms: Option<i64>,
    extended_time: bool,
) -> fmt::Result {
    write!(out, "{{\"state\":\"{}\"", reading_status(&reading))?;
    if let Reading::Live(sample) = reading {
        let [ax, ay, az] = sample.accel_mps2;
        let [gx, gy, gz] = sample.gyro_rps;
        write!(
            out,
            ",\"seq\":{},\"interval_us\":{},\"accel_mps2\":[{ax:.6e},{ay:.6e},{az:.6e}],\"gyro_rps\":[{gx:.6e},{gy:.6e},{gz:.6e}]",
            sample.sequence, sample.sample_interval_us,
        )?;
        timestamp_json(
            out,
            sample.timestamp_us,
            sample.is_capture_time,
            extended_time,
        )?;
        utc_json(out, utc_ms)?;
    }
    out.write_char('}')
}

fn gnss_json(
    out: &mut dyn Write,
    reading: Reading<gnss::GnssFix>,
    utc_ms: Option<i64>,
    extended_time: bool,
) -> fmt::Result {
    write!(out, "{{\"state\":\"{}\"", reading_status(&reading))?;
    if let Reading::Live(fix) = reading {
        write!(
            out,
            ",\"seq\":{},\"lat_deg\":{:.7},\"lon_deg\":{:.7},\"speed_mps\":{:.6e}",
            fix.sequence, fix.latitude_deg, fix.longitude_deg, fix.speed_mps,
        )?;
        timestamp_json(out, fix.timestamp_us, fix.is_capture_time, extended_time)?;
        utc_json(out, utc_ms)?;
    }
    out.write_char('}')
}

fn timestamp_json(
    out: &mut dyn Write,
    time_us: u64,
    is_capture_time: bool,
    extended_time: bool,
) -> fmt::Result {
    write!(out, ",\"received_ms\":{}", time_us / 1_000)?;
    if extended_time {
        let source = if is_capture_time {
            "capture"
        } else {
            "reception"
        };
        write!(
            out,
            ",\"timestamp_us\":{time_us},\"time_source\":\"{source}\""
        )?;
    }
    Ok(())
}

fn encode(
    out: &mut impl Write,
    snapshot: &Snapshot,
    scales: Option<[Option<[u32; 2]>; 2]>,
    tile: Option<Key>,
    extended_time: bool,
) -> fmt::Result {
    out.write_str("{\"v\":1,")?;
    if let Some(scales) = scales {
        // Legacy clients still request v2. The array advertises v3, with
        // null for each source that requires the float payload.
        out.write_str("\"export_version\":2,\"imu_scale_bits\":[")?;
        for (index, scale) in scales.into_iter().enumerate() {
            if index != 0 {
                out.write_char(',')?;
            }
            match scale {
                Some([accel, gyro]) => write!(out, "[{accel},{gyro}]")?,
                None => out.write_str("null")?,
            }
        }
        out.write_str("],")?;
    }
    let utc = |time| snapshot.clock.and_then(|anchor| anchor.utc_ms(time));
    out.write_str("\"imu\":[")?;
    for (index, reading) in snapshot.imu.into_iter().enumerate() {
        if index != 0 {
            out.write_char(',')?;
        }
        let absolute = match reading {
            Reading::Live(sample) => utc(sample.timestamp_us / 1_000),
            _ => None,
        };
        imu_json(out, reading, absolute, extended_time)?;
    }
    out.write_str("],\"gnss\":")?;
    let absolute = match snapshot.gnss {
        Reading::Live(fix) => utc(fix.timestamp_us / 1_000),
        _ => None,
    };
    gnss_json(out, snapshot.gnss, absolute, extended_time)?;
    if let Some(key) = tile {
        write!(out, ",\"tile\":[{},{},{}]", key.zoom.level(), key.x, key.y)?;
    }
    out.write_str("}\n")
}

fn build(
    snapshot: &Snapshot,
    scales: Option<[Option<[u32; 2]>; 2]>,
    tile: Option<Key>,
    extended_time: bool,
) -> io::Result<Text<768>> {
    let mut frame = Text::<768>::new();
    encode(&mut frame, snapshot, scales, tile, extended_time)
        .map_err(|_| io::ErrorKind::InvalidData)?;
    Ok(frame)
}

pub const CELLULAR_FRAME_BYTES: usize = 288;

pub fn cellular_frame(snapshot: &Snapshot) -> io::Result<Text<CELLULAR_FRAME_BYTES>> {
    let [first, second] = snapshot.imu.each_ref().map(reading_status);
    let utc = match snapshot.gnss {
        Reading::Live(fix) => snapshot
            .clock
            .and_then(|anchor| anchor.utc_ms(fix.timestamp_us / 1_000)),
        _ => None,
    };
    let mut frame = Text::new();
    write!(
        frame,
        "{{\"v\":1,\"kind\":\"summary\",\"imu\":[\"{first}\",\"{second}\"],\"imu1_received_ms\":"
    )
    .and_then(|()| match snapshot.imu[0] {
        Reading::Live(sample) => write!(frame, "{}", sample.timestamp_us / 1_000),
        _ => frame.write_str("null"),
    })
    .and_then(|()| frame.write_str(",\"gnss\":"))
    .and_then(|()| gnss_json(&mut frame, snapshot.gnss, utc, false))
    .and_then(|()| frame.write_str("}\n"))
    .map_err(|_| io::ErrorKind::InvalidData)?;
    Ok(frame)
}

pub fn frame(snapshot: &Snapshot) -> io::Result<Text<768>> {
    build(snapshot, None, None, true)
}

pub fn ble_frame(snapshot: &Snapshot, tile: Option<Key>) -> io::Result<Text<768>> {
    build(snapshot, None, tile, false)
}

pub fn export_frame(
    snapshot: &Snapshot,
    scales: [Option<[u32; 2]>; 2],
    tile: Option<Key>,
) -> io::Result<Text<768>> {
    build(snapshot, Some(scales), tile, false)
}

pub fn write_usb(output: &mut impl io::Write, snapshot: &Snapshot) -> io::Result<()> {
    output.write_all(frame(snapshot)?.bytes())?;
    output.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cellular_summary_is_bounded_and_contains_no_raw_imu_data() {
        let sample = Snapshot {
            imu: [
                Reading::Live(imu::ImuSample {
                    sequence: u64::MAX,
                    timestamp_us: u64::MAX,
                    is_capture_time: true,
                    sample_interval_us: 10_000,
                    accel_mps2: [1.0; 3],
                    gyro_rps: [2.0; 3],
                }),
                Reading::Fault,
            ],
            gnss: Reading::Live(gnss::GnssFix {
                sequence: u64::MAX,
                timestamp_us: u64::MAX,
                is_capture_time: true,
                receiver_utc_ms: None,
                latitude_deg: -90.0,
                longitude_deg: -180.0,
                speed_mps: f32::MAX,
                course_rad: None,
            }),
            clock: crate::clock::ClockAnchor::new(u64::MAX / 1_000, -1),
        };
        let frame = cellular_frame(&sample).unwrap();
        let line = std::str::from_utf8(frame.bytes()).unwrap();
        assert!(line.starts_with("{\"v\":1,\"kind\":\"summary\",\"imu\":[\"live\",\"fault\"],"));
        assert!(line.contains("\"imu1_received_ms\":18446744073709551"));
        assert!(line.contains("\"utc_ms\":-1"));
        assert!(!line.contains("timestamp_us"));
        assert!(!line.contains("time_source"));
        assert!(!line.contains("accel_mps2"));
        assert!(!line.contains("gyro_rps"));
        assert!(line.ends_with("}}\n"));
    }

    #[test]
    fn wire_preserves_sensor_order_and_omits_unavailable_coordinates() {
        let snapshot = Snapshot {
            imu: [Reading::Fault, Reading::Waiting],
            gnss: Reading::Stale,
            clock: None,
        };
        assert_eq!(
            frame(&snapshot).unwrap().bytes(),
            b"{\"v\":1,\"imu\":[{\"state\":\"fault\"},{\"state\":\"waiting\"}],\"gnss\":{\"state\":\"stale\"}}\n",
        );
    }

    #[test]
    fn usb_reports_capture_time_and_ble_export_fits_the_legacy_frame() {
        let sample = imu::ImuSample {
            sequence: 12_008,
            timestamp_us: 120_014_500,
            is_capture_time: true,
            sample_interval_us: 10_000,
            accel_mps2: [-9.81234, -0.12345, -1.23456],
            gyro_rps: [-0.00123, -0.04567, -0.08901],
        };
        let snapshot = Snapshot {
            imu: [Reading::Live(sample); 2],
            gnss: Reading::Live(gnss::GnssFix {
                sequence: 240,
                timestamp_us: 120_013_000,
                is_capture_time: true,
                receiver_utc_ms: None,
                latitude_deg: -45.1234567,
                longitude_deg: -76.1234567,
                speed_mps: 1.23456,
                course_rad: None,
            }),
            clock: crate::clock::ClockAnchor::new(100_000, 1_767_225_600_000),
        };
        let usb = frame(&snapshot).unwrap();
        let json = std::str::from_utf8(usb.bytes()).unwrap();
        assert!(json.contains(
            "\"received_ms\":120014,\"timestamp_us\":120014500,\"time_source\":\"capture\""
        ));
        let tile = Key {
            zoom: Default::default(),
            x: 123_456,
            y: 234_567,
        };
        let ble =
            export_frame(&snapshot, [Some([0x3c23_d70a, 0x3c23_d70a]); 2], Some(tile)).unwrap();
        let json = std::str::from_utf8(ble.bytes()).unwrap();
        assert!(json.contains("\"tile\":[18,123456,234567]"));
        assert!(json.contains("\"received_ms\":120014"));
        assert!(!json.contains("time_source"));
    }
}
