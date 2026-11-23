//! Transport-independent snapshots.
use crate::{
    text::Text,
    tracker::{reading_status, Reading},
};
use std::fmt::{self, Write};
use std::io;

fn utc_json(out: &mut impl Write, utc_ms: Option<i64>) -> fmt::Result {
    match utc_ms {
        Some(utc_ms) => write!(out, ",\"utc_ms\":{utc_ms}"),
        None => out.write_str(",\"utc_ms\":null"),
    }
}

fn imu_json(out: &mut impl Write, reading: Reading<imu::ImuSample>, utc_ms: Option<i64>) -> fmt::Result {
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
    use crate::tracker::Snapshot;

    fn gnss_json(out: &mut impl Write, reading: Reading<gnss::GnssFix>, utc_ms: Option<i64>) -> fmt::Result {
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
        out.write_str("}\n")
    }

    fn build(snapshot: &Snapshot, scales: Option<[Option<[u32; 2]>; 2]>) -> io::Result<Text<768>> {
        let mut frame = Text::<768>::new();
        encode(&mut frame, snapshot, scales).map_err(|_| io::ErrorKind::InvalidData)?;
        Ok(frame)
    }

    pub fn frame(snapshot: &Snapshot) -> io::Result<Text<768>> {
        build(snapshot, None)
    }

    pub fn export_frame(snapshot: &Snapshot, scales: [Option<[u32; 2]>; 2]) -> io::Result<Text<768>> {
        build(snapshot, Some(scales))
    }

    #[cfg(test)]
    mod tests {
        use super::*;

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

#[cfg(feature = "dashboard")]
pub use version::export_frame;
pub use version::frame;
