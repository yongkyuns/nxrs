//! Transport-independent snapshots.
use crate::{
    text::Text,
    tracker::{status, Reading, Snapshot},
};
use std::{
    fmt::{self, Write},
    io,
};

pub(super) type Frame = Text<768>;

fn imu_json(out: &mut impl Write, reading: Reading<imu::ImuSample>) -> fmt::Result {
    write!(out, "{{\"state\":\"{}\"", status(Some(reading)))?;
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

fn gnss_json(out: &mut impl Write, reading: Reading<gnss::GnssFix>) -> fmt::Result {
    write!(out, "{{\"state\":\"{}\"", status(Some(reading)))?;
    if let Reading::Live(fix) = reading {
        write!(
            out,
            ",\"seq\":{},\"received_ms\":{},\"lat_deg\":{:.7},\"lon_deg\":{:.7},\"speed_mps\":{:.6e}",
            fix.sequence, fix.timestamp_ms, fix.latitude_deg, fix.longitude_deg, fix.speed_mps,
        )?;
    }
    out.write_char('}')
}

fn encode(out: &mut impl Write, snapshot: &Snapshot) -> fmt::Result {
    // Full configurations keep the v1 layout used by deployed peers.
    let version = if snapshot.imu[1].is_some() { 1 } else { 2 };
    write!(out, "{{\"v\":{version},\"imu\":[")?;
    for (index, reading) in snapshot.imu.into_iter().flatten().enumerate() {
        if index > 0 {
            out.write_char(',')?;
        }
        imu_json(out, reading)?;
    }
    out.write_str("],\"gnss\":")?;
    gnss_json(out, snapshot.gnss.ok_or(fmt::Error)?)?;
    out.write_str("}\n")
}

pub(super) fn frame(snapshot: &Snapshot) -> io::Result<Frame> {
    let mut frame = Frame::new();
    encode(&mut frame, snapshot).map_err(|_| io::ErrorKind::InvalidData)?;
    Ok(frame)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wire_preserves_sensor_order_and_omits_unavailable_coordinates() {
        let mut snapshot = Snapshot {
            imu: [Some(Reading::Fault), Some(Reading::Waiting)],
            gnss: Some(Reading::Stale),
        };
        assert_eq!(
            frame(&snapshot).unwrap().as_ref(),
            b"{\"v\":1,\"imu\":[{\"state\":\"fault\"},{\"state\":\"waiting\"}],\"gnss\":{\"state\":\"stale\"}}\n",
        );
        snapshot.imu[1] = None;
        snapshot.gnss = Some(Reading::Waiting);
        assert_eq!(
            frame(&snapshot).unwrap().as_ref(),
            b"{\"v\":2,\"imu\":[{\"state\":\"fault\"}],\"gnss\":{\"state\":\"waiting\"}}\n",
        );
    }
}
