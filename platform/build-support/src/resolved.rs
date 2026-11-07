//! Versioned, verified profile receipts for one concrete Cargo build.
use crate::{composition, hash, hash_bytes, profile::Profile, read, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

const VERSION: u32 = 1;

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    version: u32,
    workspace: PathBuf,
    profile: String,
    target: String,
    os: String,
    cli_overlays: Vec<PathBuf>,
    data: Value,
    graph: Value,
    semantic_digest: String,
    raw_fingerprint: Option<String>,
    dependencies: Vec<Dependency>,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Dependency {
    path: PathBuf,
    sha256: String,
}

fn canonical_root(root: &Path) -> Result<PathBuf> {
    root.canonicalize()
        .map_err(|e| format!("workspace {}: {e}", root.display()))
}

fn graph(profile: &Profile) -> Result<Value> {
    profile
        .composition()?
        .map(|value| serde_json::to_value(value).map_err(|e| e.to_string()))
        .transpose()
        .map(|value| value.unwrap_or(Value::Null))
}

fn semantic_digest(profile: &Profile, graph: &Value) -> String {
    hash_bytes(
        json!({ "graph": graph, "target": profile.data.get("target") })
            .to_string()
            .as_bytes(),
    )
}

fn dependencies(root: &Path, profile: &Profile) -> Result<Vec<Dependency>> {
    let mut paths: Vec<PathBuf> = profile.source_paths().cloned().collect();
    if profile.binding("imu") == "nuttx" {
        paths.push(root.join(composition::BINDING_SCHEMA_PATH));
    }
    for capability in profile.data["hal"]
        .as_object()
        .ok_or("profile HAL table missing")?
        .keys()
    {
        paths.push(root.join("hal").join(capability).join("Cargo.toml"));
    }
    paths.sort();
    paths.dedup();
    paths
        .into_iter()
        .map(|path| {
            let path = path
                .canonicalize()
                .map_err(|e| format!("dependency {}: {e}", path.display()))?;
            Ok(Dependency {
                sha256: hash(&path)?,
                path,
            })
        })
        .collect()
}

fn receipt(root: &Path, profile: &Profile, target: &str, os: &str) -> Result<Receipt> {
    profile.validate_cargo_target(target, os)?;
    let graph = graph(profile)?;
    Ok(Receipt {
        version: VERSION,
        workspace: canonical_root(root)?,
        profile: profile.name().to_owned(),
        target: target.to_owned(),
        os: os.to_owned(),
        cli_overlays: profile.cli_overlays.clone(),
        data: profile.data.clone(),
        semantic_digest: semantic_digest(profile, &graph),
        raw_fingerprint: profile.input_fingerprint(),
        dependencies: dependencies(root, profile)?,
        graph,
    })
}

/// Atomically write the receipt, leaving its timestamp untouched when bytes match.
pub fn write(path: &Path, root: &Path, profile: &Profile, target: &str, os: &str) -> Result<()> {
    let value = receipt(root, profile, target, os)?;
    let mut bytes = serde_json::to_vec_pretty(&value).map_err(|e| e.to_string())?;
    bytes.push(b'\n');
    if fs::read(path).ok().as_deref() == Some(bytes.as_slice()) {
        return Ok(());
    }
    let parent = path.parent().ok_or("receipt path has no parent")?;
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let (temporary, mut file) = (0..100)
        .find_map(|_| {
            let candidate = parent.join(format!(
                ".{}.{}.{}.tmp",
                path.file_name()?.to_string_lossy(),
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed),
            ));
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&candidate)
            {
                Ok(file) => Some(Ok((candidate, file))),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => None,
                Err(e) => Some(Err(e)),
            }
        })
        .ok_or("could not allocate temporary receipt")?
        .map_err(|e| e.to_string())?;
    let result = (|| {
        file.write_all(&bytes).map_err(|e| e.to_string())?;
        file.sync_all().map_err(|e| e.to_string())?;
        drop(file);
        fs::rename(&temporary, path).map_err(|e| format!("{}: {e}", path.display()))
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}

/// Load only if the receipt still resolves identically for this workspace and build target.
pub fn load(
    path: &Path,
    root: &Path,
    platform: Option<&str>,
    target: &str,
    os: &str,
) -> Result<Profile> {
    let bytes = read(path)?;
    let saved: Receipt =
        serde_json::from_str(&bytes).map_err(|e| format!("{}: {e}", path.display()))?;
    if saved.version != VERSION {
        return Err(format!(
            "unsupported resolved profile receipt version {}",
            saved.version
        ));
    }
    if saved.workspace != canonical_root(root)? {
        return Err("resolved profile receipt belongs to another workspace".into());
    }
    if saved.target != target || saved.os != os {
        return Err("resolved profile receipt target does not match this Cargo build".into());
    }
    if platform.is_some_and(|name| name != saved.profile) {
        return Err("resolved profile receipt does not match the selected platform".into());
    }
    let resolved = Profile::resolve_with_overlays(root, &saved.profile, &saved.cli_overlays)?;
    let current = receipt(root, &resolved, target, os)?;
    if current.dependencies != saved.dependencies {
        return Err("stale profile dependency list or content".into());
    }
    if current.data != saved.data
        || current.graph != saved.graph
        || current.semantic_digest != saved.semantic_digest
        || current.raw_fingerprint != saved.raw_fingerprint
    {
        return Err("resolved profile receipt is stale or does not match its inputs".into());
    }
    Ok(resolved)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};

    fn fixture() -> (PathBuf, PathBuf) {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap();
        let receipt = std::env::temp_dir().join(format!(
            "nxrs-profile-{}-{}.json",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        (root, receipt)
    }

    #[test]
    fn receipt_verifies_selection_target_and_identical_write_is_noop() {
        let (root, path) = fixture();
        let profile = Profile::resolve(&root, "linux").unwrap();
        write(&path, &root, &profile, "x86_64-unknown-linux-gnu", "linux").unwrap();
        let stamp = fs::metadata(&path).unwrap().modified().unwrap();
        write(&path, &root, &profile, "x86_64-unknown-linux-gnu", "linux").unwrap();
        assert_eq!(fs::metadata(&path).unwrap().modified().unwrap(), stamp);
        assert!(load(
            &path,
            &root,
            Some("macos"),
            "x86_64-unknown-linux-gnu",
            "linux"
        )
        .is_err());
        assert!(load(&path, &root, Some("linux"), "aarch64-apple-darwin", "linux").is_err());
        assert_eq!(
            load(
                &path,
                &root,
                Some("linux"),
                "x86_64-unknown-linux-gnu",
                "linux"
            )
            .unwrap()
            .name(),
            "linux"
        );
        let _ = fs::remove_file(path);
    }

    #[test]
    fn receipt_rejects_stale_dependency() {
        let (root, path) = fixture();
        let profile = Profile::resolve(&root, "linux").unwrap();
        write(&path, &root, &profile, "x86_64-unknown-linux-gnu", "linux").unwrap();
        let mut saved: Value = serde_json::from_str(&read(&path).unwrap()).unwrap();
        saved["dependencies"][0]["sha256"] = json!("0".repeat(64));
        fs::write(&path, serde_json::to_vec(&saved).unwrap()).unwrap();
        assert!(load(
            &path,
            &root,
            Some("linux"),
            "x86_64-unknown-linux-gnu",
            "linux"
        )
        .unwrap_err()
        .contains("stale profile dependency"));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn one_receipt_path_can_return_to_an_earlier_profile() {
        let (root, path) = fixture();
        let a = Profile::resolve(&root, "linux").unwrap();
        let b = Profile::resolve(&root, "macos").unwrap();
        write(&path, &root, &a, "x86_64-unknown-linux-gnu", "linux").unwrap();
        write(&path, &root, &b, "aarch64-apple-darwin", "macos").unwrap();
        assert!(load(
            &path,
            &root,
            Some("linux"),
            "x86_64-unknown-linux-gnu",
            "linux"
        )
        .is_err());
        write(&path, &root, &a, "x86_64-unknown-linux-gnu", "linux").unwrap();
        assert_eq!(
            load(
                &path,
                &root,
                Some("linux"),
                "x86_64-unknown-linux-gnu",
                "linux"
            )
            .unwrap()
            .name(),
            "linux"
        );
        let _ = fs::remove_file(path);
    }
}
