//! Transport-independent snapshots.
use crate::{
    text::Text,
    tracker::{reading_status, Reading},
};
use std::fmt::{self, Write};
use std::io;

fn imu_json(out: &mut impl Write, reading: Reading<imu::ImuSample>) -> fmt::Result {
    write!(out, "{{\"state\":\"{}\"", reading_status(&reading))?;
    if let Reading::Live(sample) = reading {
        let [ax, ay, az] = sample.accel_mps2;
        let [gx, gy, gz] = sample.gyro_rps;
        write!(
            out,
            ",\"seq\":{},\"received_ms\":{},\"interval_us\":{},\"accel_mps2\":[{ax:.6e},{ay:.6e},{az:.6e}],\"gyro_rps\":[{gx:.6e},{gy:.6e},{gz:.6e}]",
            sample.sequence, sample.timestamp_ms, sample.sample_interval_us,
        )?;
    }
    out.write_char('}')
}

#[cfg(feature = "dashboard")]
mod version {
    use super::*;
    use crate::tracker::Snapshot;

    fn gnss_json(out: &mut impl Write, reading: Reading<gnss::GnssFix>) -> fmt::Result {
        write!(out, "{{\"state\":\"{}\"", reading_status(&reading))?;
        if let Reading::Live(fix) = reading {
            write!(
                out,
                ",\"seq\":{},\"received_ms\":{},\"lat_deg\":{:.7},\"lon_deg\":{:.7},\"speed_mps\":{:.6e}",
                fix.sequence, fix.timestamp_ms, fix.latitude_deg, fix.longitude_deg, fix.speed_mps,
            )?;
        }
        out.write_char('}')
    }

    fn encode(out: &mut impl Write, snapshot: &Snapshot, export: bool) -> fmt::Result {
        out.write_str("{\"v\":1,")?;
        if export {
            out.write_str("\"export_version\":2,")?;
        }
        out.write_str("\"imu\":[")?;
        imu_json(out, snapshot.imu[0])?;
        out.write_char(',')?;
        imu_json(out, snapshot.imu[1])?;
        out.write_str("],\"gnss\":")?;
        gnss_json(out, snapshot.gnss)?;
        out.write_str("}\n")
    }

    pub fn frame(snapshot: &Snapshot) -> io::Result<Text<768>> {
        let mut frame = Text::<768>::new();
        encode(&mut frame, snapshot, false).map_err(|_| io::ErrorKind::InvalidData)?;
        Ok(frame)
    }

    pub fn export_frame(snapshot: &Snapshot) -> io::Result<Text<768>> {
        let mut frame = Text::<768>::new();
        encode(&mut frame, snapshot, true).map_err(|_| io::ErrorKind::InvalidData)?;
        Ok(frame)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn wire_preserves_sensor_order_and_omits_unavailable_coordinates() {
            let snapshot = Snapshot {
                imu: [Reading::Fault, Reading::Waiting],
                gnss: Reading::Stale,
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
            .and_then(|()| imu_json(&mut frame, snapshot.imu[0]))
            .and_then(|()| frame.write_str("]}\n"))
            .map_err(|_| io::ErrorKind::InvalidData)?;
        Ok(frame)
    }
}

#[cfg(feature = "dashboard")]
pub use version::export_frame;
pub use version::frame;
