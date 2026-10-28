//! Authenticate/list through restic, then constrain the inactive restore namespace before extraction.
use super::{hex, Config, Error};
use std::collections::{BTreeMap, BTreeSet};
pub(super) fn validate(config: &Config, bytes: &[u8]) -> Result<(), Error> {
    let mut paths = BTreeSet::new();
    let mut directories = BTreeSet::new();
    let mut leaves = BTreeMap::<String, usize>::new();
    let mut media = 0u64;
    let mut headers = 0;
    let limit = 5 + config.storage.camera_ids.len() * 2 + config.storage.max_events * 4;
    for line in bytes.split(|v| *v == b'\n').filter(|v| !v.is_empty()) {
        let value: serde_json::Value = serde_json::from_slice(line).map_err(|_| Error::Recovery)?;
        if value["struct_type"] == "snapshot" {
            headers += 1;
            if headers > 1 {
                return Err(Error::Recovery);
            }
            continue;
        }
        if value["struct_type"] != "node" {
            return Err(Error::Recovery);
        }
        let path = value["path"].as_str().ok_or(Error::Recovery)?;
        let kind = value["type"].as_str().ok_or(Error::Recovery)?;
        if !paths.insert(path.to_string()) || paths.len() > limit || path.len() > 200 {
            return Err(Error::Recovery);
        }
        let mode = value["mode"].as_u64().ok_or(Error::Recovery)?;
        if mode & 0o077 != 0 {
            return Err(Error::Recovery);
        }
        if path == "/" {
            if kind != "dir" {
                return Err(Error::Recovery);
            }
            continue;
        }
        let relative = path.strip_prefix('/').ok_or(Error::Recovery)?;
        let parts: Vec<_> = relative.split('/').collect();
        if kind == "dir" {
            match parts.as_slice() {
                ["events" | "staging" | "spool"] => {}
                ["events", id] if hex(id, 32) => {
                    directories.insert(id.to_string());
                }
                _ => return Err(Error::Recovery),
            }
            continue;
        }
        if kind != "file" {
            return Err(Error::Recovery);
        }
        let size = value["size"].as_u64().ok_or(Error::Recovery)?;
        let max = match parts.as_slice() {
            ["store.lock"] => 0,
            [name]
                if config
                    .storage
                    .camera_ids
                    .iter()
                    .any(|id| *name == format!("worker-{id}.lock")) =>
            {
                0
            }
            [name]
                if config
                    .storage
                    .camera_ids
                    .iter()
                    .any(|id| *name == format!("health-{id}.json")) =>
            {
                65536
            }
            ["events", id, name] if hex(id, 32) => {
                *leaves.entry(id.to_string()).or_default() += 1;
                match *name {
                    "clip.mp4" => {
                        media = media.checked_add(size).ok_or(Error::Recovery)?;
                        crate::store::MAX_CLIP_BYTES
                    }
                    "thumbnail.jpg" => {
                        media = media.checked_add(size).ok_or(Error::Recovery)?;
                        crate::store::MAX_THUMBNAIL_BYTES
                    }
                    "event.json" => 4096,
                    _ => return Err(Error::Recovery),
                }
            }
            _ => return Err(Error::Recovery),
        };
        if size > max {
            return Err(Error::Recovery);
        }
    }
    if headers != 1
        || directories.len() > config.storage.max_events
        || media > config.storage.max_bytes
        || directories.len() != leaves.len()
        || directories.iter().any(|id| leaves.get(id) != Some(&3))
        || ["/events", "/staging", "/spool"]
            .iter()
            .any(|path| !paths.contains(*path))
    {
        return Err(Error::Recovery);
    }
    Ok(())
}
