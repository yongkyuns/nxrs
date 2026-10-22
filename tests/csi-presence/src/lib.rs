//! Read-only adapter for the pinned Espressif wifi_sensing_demo HMS protocol.
#![forbid(unsafe_code)]

use presence_service::{Calibration, Sample};
use serde::Deserialize;
use std::io::BufRead;

pub const MAX_LINE_BYTES: usize = 2_048;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Peer([u8; 6]);

impl Peer {
    pub fn parse(value: &str) -> Result<Self, &'static str> {
        let mut bytes = [0; 6];
        if value.len() != 17 {
            return Err("expected a six-byte peer MAC address");
        }
        for (index, octet) in value.split(':').enumerate() {
            if index >= bytes.len()
                || octet.len() != 2
                || !octet.bytes().all(|byte| byte.is_ascii_hexdigit())
            {
                return Err("invalid peer MAC address");
            }
            bytes[index] = u8::from_str_radix(octet, 16).map_err(|_| "invalid peer MAC address")?;
        }
        Ok(Self(bytes))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Report {
    /// The operator must retire the old stream before starting a new session.
    Hello,
    Sample(Sample),
}

#[derive(Deserialize)]
struct WireSample {
    ts_ms: u64,
    motion_status: bool,
    presence_someone_status: bool,
    presence_ready: bool,
    train_thresholds_valid: bool,
    train_status_name: String,
    init_stage_name: String,
}

pub fn parse_line(line: &[u8], selected: Peer) -> Result<Option<Report>, &'static str> {
    if line.len() > MAX_LINE_BYTES {
        return Err("input line exceeds the 2048-byte limit");
    }
    let Some(json) = line.strip_prefix(b"HMS:") else {
        return Ok(None);
    };
    let value: serde_json::Value =
        serde_json::from_slice(json).map_err(|_| "malformed HMS JSON")?;
    match value.get("type").and_then(|value| value.as_str()) {
        Some("hello") => Ok(Some(Report::Hello)),
        Some("sample") => {
            let peer = value
                .get("mac")
                .and_then(|value| value.as_str())
                .ok_or("sample has no peer identity")?;
            if Peer::parse(peer)? != selected {
                return Ok(None);
            }
            let wire: WireSample =
                serde_json::from_value(value).map_err(|_| "incomplete HMS sample")?;
            let calibration = match wire.train_status_name.as_str() {
                "PROGRESS" => Calibration::InProgress,
                "NONE" => Calibration::Required,
                "COMPLETE" => {
                    if wire.presence_ready
                        && wire.train_thresholds_valid
                        && wire.init_stage_name == "STABLE"
                    {
                        Calibration::Ready
                    } else {
                        Calibration::Required
                    }
                }
                _ => return Err("unsupported HMS training state"),
            };
            Ok(Some(Report::Sample(Sample {
                source_timestamp_ms: wire.ts_ms,
                calibration,
                motion_detected: wire.motion_status,
                presence_detected: wire.presence_someone_status,
            })))
        }
        Some("error") => Err("Espressif demo reported an error"),
        Some("ack" | "runtime" | "config") => Ok(None),
        _ => Err("unsupported HMS message type"),
    }
}

/// Bounded even for an unterminated line. An oversized line terminates this
/// evaluation session; its suffix is never admitted as a new report.
pub fn read_line(reader: &mut impl BufRead) -> std::io::Result<Option<Vec<u8>>> {
    let mut line = Vec::with_capacity(MAX_LINE_BYTES);
    let count =
        std::io::Read::take(reader, (MAX_LINE_BYTES + 1) as u64).read_until(b'\n', &mut line)?;
    if count > MAX_LINE_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "input line exceeds the 2048-byte limit",
        ));
    }
    if count == 0 {
        return Ok(None);
    }
    // An incomplete tail may be a torn capture, not a complete observation.
    if line.last() != Some(&b'\n') {
        return Err(std::io::Error::new(
            std::io::ErrorKind::UnexpectedEof,
            "incomplete input line",
        ));
    }
    Ok(Some(line))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn peer() -> Peer {
        Peer::parse("02:00:00:00:00:01").unwrap()
    }

    fn sample() -> serde_json::Value {
        json!({"type":"sample", "ts_ms":100, "mac":"02:00:00:00:00:01",
            "motion_status":false, "presence_someone_status":true,
            "presence_ready":true, "train_thresholds_valid":true,
            "train_status_name":"COMPLETE", "init_stage_name":"STABLE"})
    }

    fn parse(value: serde_json::Value) -> Result<Option<Report>, &'static str> {
        parse_line(format!("HMS:{value}\n").as_bytes(), peer())
    }

    #[test]
    fn accepts_demo_sample_without_inventing_confidence_or_identity() {
        let Some(Report::Sample(value)) = parse(sample()).unwrap() else {
            panic!()
        };
        assert_eq!(value.source_timestamp_ms, 100);
        assert_eq!(value.calibration, Calibration::Ready);
        assert!(value.presence_detected);
        assert!(!value.motion_detected);
    }

    #[test]
    fn filters_other_peers_before_admitting_any_evidence() {
        let mut value = sample();
        value["mac"] = json!("02:00:00:00:00:02");
        value.as_object_mut().unwrap().remove("presence_ready");
        assert_eq!(parse(value), Ok(None));
        assert_eq!(
            Peer::parse("AA:BB:CC:DD:EE:FF"),
            Peer::parse("aa:bb:cc:dd:ee:ff")
        );
    }

    #[test]
    fn malformed_or_incomplete_samples_fail_closed() {
        for key in [
            "ts_ms",
            "mac",
            "motion_status",
            "presence_ready",
            "train_thresholds_valid",
        ] {
            let mut value = sample();
            value.as_object_mut().unwrap().remove(key);
            assert!(parse(value).is_err());
        }
        for timestamp in [json!(-1), json!(1.5), json!("100")] {
            let mut value = sample();
            value["ts_ms"] = timestamp;
            assert!(parse(value).is_err());
        }
        assert!(parse_line(b"HMS:{", peer()).is_err());
    }

    #[test]
    fn presence_flags_do_not_override_missing_calibration_or_readiness() {
        for key in ["presence_ready", "train_thresholds_valid"] {
            let mut value = sample();
            value[key] = json!(false);
            let Some(Report::Sample(value)) = parse(value).unwrap() else {
                panic!()
            };
            assert_eq!(value.calibration, Calibration::Required);
        }
        for (name, expected) in [
            ("NONE", Calibration::Required),
            ("PROGRESS", Calibration::InProgress),
        ] {
            let mut value = sample();
            value["train_status_name"] = json!(name);
            let Some(Report::Sample(value)) = parse(value).unwrap() else {
                panic!()
            };
            assert_eq!(value.calibration, expected);
        }
    }

    #[test]
    fn ignores_console_logs_and_recognizes_boot_handshake() {
        assert_eq!(parse_line(b"I (0) wifi: connected\n", peer()), Ok(None));
        assert_eq!(
            parse_line(b"HMS:{\"type\":\"hello\"}\n", peer()),
            Ok(Some(Report::Hello))
        );
        assert!(parse_line(b"HMS:{\"type\":\"error\"}\n", peer()).is_err());
    }

    #[test]
    fn bounds_unterminated_input_and_rejects_partial_tail() {
        let mut input = std::io::Cursor::new(vec![b'x'; MAX_LINE_BYTES + 10_000]);
        assert_eq!(
            read_line(&mut input).unwrap_err().kind(),
            std::io::ErrorKind::InvalidData
        );
        assert_eq!(input.position(), (MAX_LINE_BYTES + 1) as u64);
        assert!(read_line(&mut std::io::Cursor::new(b"HMS:{}")).is_err());
        assert!(read_line(&mut std::io::Cursor::new(b"")).unwrap().is_none());
    }

    #[test]
    fn rejects_malformed_peer_identities() {
        for value in [
            "",
            "00000000000000000",
            "02:00:00:00:00:0g",
            "02-00-00-00-00-01",
        ] {
            assert!(Peer::parse(value).is_err());
        }
    }
}
