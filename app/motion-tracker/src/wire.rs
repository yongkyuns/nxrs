//! Transport-independent snapshots.
use crate::{
    text::Text,
    tracker::{Reading, reading_status},
};
use std::fmt::{self, Write};
use std::io;

fn utc_json(out: &mut impl Write, utc_ms: Option<i64>) -> fmt::Result {
    match utc_ms {
        Some(utc_ms) => write!(out, ",\"utc_ms\":{utc_ms}"),
        None => out.write_str(",\"utc_ms\":null"),
    }
}

fn imu_json(
    out: &mut impl Write,
    reading: Reading<imu::ImuSample>,
    utc_ms: Option<i64>,
) -> fmt::Result {
    write!(out, "{{\"state\":\"{}\"", reading_status(&reading))?;
    if let Reading::Live(sample) = reading {
        let [ax, ay, az] = sample.accel_mps2;
        let [gx, gy, gz] = sample.gyro_rps;
        write!(
            out,
            ",\"seq\":{},\"received_ms\":{},\"interval_us\":{},\"accel_mps2\":[{ax:.6e},{ay:.6e},{az:.6e}],\"gyro_rps\":[{gx:.6e},{gy:.6e},{gz:.6e}]",
            sample.sequence, sample.timestamp_ms, sample.sample_interval_us,
        )?;
        utc_json(out, utc_ms)?;
    }
    out.write_char('}')
}

#[cfg(feature = "dashboard")]
mod version {
    use super::*;
    use crate::{map::Key, tracker::Snapshot};

    fn gnss_json(
        out: &mut impl Write,
        reading: Reading<gnss::GnssFix>,
        utc_ms: Option<i64>,
    ) -> fmt::Result {
        write!(out, "{{\"state\":\"{}\"", reading_status(&reading))?;
        if let Reading::Live(fix) = reading {
            write!(
                out,
                ",\"seq\":{},\"received_ms\":{},\"lat_deg\":{:.7},\"lon_deg\":{:.7},\"speed_mps\":{:.6e}",
                fix.sequence, fix.timestamp_ms, fix.latitude_deg, fix.longitude_deg, fix.speed_mps,
            )?;
            utc_json(out, utc_ms)?;
        }
        out.write_char('}')
    }

    fn encode(
        out: &mut impl Write,
        snapshot: &Snapshot,
        scales: Option<[Option<[u32; 2]>; 2]>,
        tile: Option<Key>,
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
                Reading::Live(sample) => utc(sample.timestamp_ms),
                _ => None,
            };
            imu_json(out, reading, absolute)?;
        }
        out.write_str("],\"gnss\":")?;
        let absolute = match snapshot.gnss {
            Reading::Live(fix) => utc(fix.timestamp_ms),
            _ => None,
        };
        gnss_json(out, snapshot.gnss, absolute)?;
        if let Some(key) = tile {
            write!(out, ",\"tile\":[{},{},{}]", key.zoom.level(), key.x, key.y)?;
        }
        out.write_str("}\n")
    }

    fn build(
        snapshot: &Snapshot,
        scales: Option<[Option<[u32; 2]>; 2]>,
        tile: Option<Key>,
    ) -> io::Result<Text<768>> {
        let mut frame = Text::<768>::new();
        encode(&mut frame, snapshot, scales, tile).map_err(|_| io::ErrorKind::InvalidData)?;
        Ok(frame)
    }

    pub fn frame(snapshot: &Snapshot) -> io::Result<Text<768>> {
        ble_frame(snapshot, None)
    }

    pub fn ble_frame(snapshot: &Snapshot, tile: Option<Key>) -> io::Result<Text<768>> {
        build(snapshot, None, tile)
    }

    #[cfg(feature = "ble-link")]
    pub fn export_frame(
        snapshot: &Snapshot,
        scales: [Option<[u32; 2]>; 2],
        tile: Option<Key>,
    ) -> io::Result<Text<768>> {
        build(snapshot, Some(scales), tile)
    }

    #[cfg(feature = "cellular-uplink")]
    pub mod cellular {
        use super::*;

        pub const MAX_BYTES: usize = 256;

        #[derive(Clone, Copy)]
        pub struct Summary {
            imu: [&'static str; 2],
            gnss: Reading<gnss::GnssFix>,
            utc_ms: Option<i64>,
        }

        impl From<Snapshot> for Summary {
            fn from(snapshot: Snapshot) -> Self {
                Self {
                    imu: snapshot.imu.map(|reading| reading_status(&reading)),
                    gnss: snapshot.gnss,
                    utc_ms: match snapshot.gnss {
                        Reading::Live(fix) => snapshot
                            .clock
                            .and_then(|anchor| anchor.utc_ms(fix.timestamp_ms)),
                        _ => None,
                    },
                }
            }
        }

        pub fn frame(summary: &Summary) -> io::Result<Text<MAX_BYTES>> {
            let [first, second] = summary.imu;
            let mut text = Text::new();
            write!(
                text,
                "{{\"v\":1,\"kind\":\"summary\",\"imu\":[\"{first}\",\"{second}\"],\"gnss\":"
            )
            .and_then(|()| gnss_json(&mut text, summary.gnss, summary.utc_ms))
            .and_then(|()| text.write_str("}\n"))
            .map_err(|_| io::ErrorKind::InvalidData)?;
            Ok(text)
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[cfg(feature = "cellular-uplink")]
        #[test]
        fn cellular_summary_fits_the_budget_with_large_sensor_values() {
            let sample = Snapshot {
                imu: [Reading::Stale, Reading::Fault],
                gnss: Reading::Live(gnss::GnssFix {
                    sequence: u64::MAX,
                    timestamp_ms: u64::MAX,
                    receiver_utc_ms: None,
                    latitude_deg: -90.0,
                    longitude_deg: -180.0,
                    speed_mps: f32::MAX,
                    course_rad: None,
                }),
                clock: None,
            };
            let frame = cellular::frame(&sample.into()).unwrap();
            let frame = std::str::from_utf8(frame.bytes()).unwrap();
            assert!(frame.len() <= cellular::MAX_BYTES);
            assert!(
                frame.starts_with("{\"v\":1,\"kind\":\"summary\",\"imu\":[\"stale\",\"fault\"],")
            );
            assert!(frame.contains(&format!("\"seq\":{}", u64::MAX)));
            assert_eq!(frame.lines().count(), 1);
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
    }
}

#[cfg(not(feature = "dashboard"))]
mod version {
    use super::*;
    use navigation::observations::Snapshot;

    pub fn frame(snapshot: &Snapshot<1>) -> io::Result<Text<768>> {
        let mut frame = Text::<768>::new();
        frame
            .write_str("{\"v\":2,\"imu\":[")
            .and_then(|()| imu_json(&mut frame, snapshot.imu[0], None))
            .and_then(|()| frame.write_str("]}\n"))
            .map_err(|_| io::ErrorKind::InvalidData)?;
        Ok(frame)
    }
}

#[cfg(feature = "ble-link")]
pub use version::export_frame;
pub use version::frame;

#[cfg(feature = "ble-link")]
pub use version::ble_frame;

#[cfg(feature = "cellular-uplink")]
pub use version::cellular;
