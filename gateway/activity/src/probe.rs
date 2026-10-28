//! Test-only entry points. No storage, credentials, network, children or native SDK.
use crate::store::{MAX_CLIP_BYTES, MAX_THUMBNAIL_BYTES};
use std::path::Path;

pub fn recording_csv(bytes: &[u8]) -> bool {
    let directory = Path::new("/nxrs-fuzz-source");
    let Ok(record) = crate::parser::segment(bytes, directory) else {
        return false;
    };
    assert_eq!(record.path.parent(), Some(directory));
    assert!((10..=10_000).contains(&record.duration_ms));
    let name = record.path.file_name().unwrap().to_str().unwrap();
    assert_eq!(name.len(), 14);
    assert!(name.starts_with("part-") && name.ends_with(".ts"));
    assert!(name[5..11].parse::<u32>().unwrap() <= 999_999);
    true
}

pub fn recording_event(bytes: &[u8]) -> bool {
    let Ok(event) = crate::store::parse_event(bytes) else {
        return false;
    };
    assert_eq!(event.event_id.len(), 32);
    assert!(event
        .event_id
        .bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)));
    assert!(!event.camera_id.is_empty() && event.camera_id.len() <= 64);
    assert!(event
        .camera_id
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'));
    assert_eq!(event.kind, "motion");
    assert!(event.started_at_ms > 0 && event.ended_at_ms > event.started_at_ms);
    assert!(event.ended_at_ms - event.started_at_ms <= 120_000);
    assert!((1..=MAX_CLIP_BYTES).contains(&event.clip_bytes));
    assert!((1..=MAX_THUMBNAIL_BYTES).contains(&event.thumbnail_bytes));
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn production_record_parsers_accept_deep_seeds_and_refuse_escapes() {
        assert!(recording_csv(b"part-000001.ts,0,2\n"));
        assert!(recording_csv(b"/nxrs-fuzz-source/part-000001.ts,0,2\n"));
        for bad in [
            b"/private/part-000001.ts,0,2\n".as_slice(),
            b"../part-000001.ts,0,2\n",
            b"part-000001.ts,NaN,2\n",
            b"part-000001.ts,0,11\n",
            b"part-000001.ts,0,2",
        ] {
            assert!(!recording_csv(bad));
        }
        let valid=br#"{"event_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","camera_id":"cam","kind":"motion","started_at_ms":1,"ended_at_ms":2001,"clip_bytes":1024,"thumbnail_bytes":64,"kept":false,"interrupted":false}"#;
        assert!(recording_event(valid));
        let mut value: serde_json::Value = serde_json::from_slice(valid).unwrap();
        for (key, bad) in [
            ("ended_at_ms", 0u64),
            ("ended_at_ms", u64::MAX),
            ("clip_bytes", u64::MAX),
            ("thumbnail_bytes", 0),
        ] {
            let mut changed = value.clone();
            changed[key] = bad.into();
            assert!(!recording_event(&serde_json::to_vec(&changed).unwrap()));
        }
        value["event_id"] = "../escape".into();
        assert!(!recording_event(&serde_json::to_vec(&value).unwrap()));
        assert!(!recording_csv(&[b'a'; 1025]));
        assert!(!recording_event(&[b'a'; 4097]));
    }
}
