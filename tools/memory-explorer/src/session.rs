//! Portable, integrity-checked inputs for one diagnostic recording.
//!
//! The manifest binds retained host files. It does not attest the image running
//! on a target or establish a shared clock between independent observations.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{Read, Take};
use std::path::{Component, Path, PathBuf};

use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::{Error, Result};

const MIB: u64 = 1024 * 1024;
const MAX_MANIFEST: u64 = MIB;
const MAX_SESSION: u64 = 1024 * MIB;
const MAX_FILES: usize = 256;
const MAX_SNAPSHOTS: usize = 128;

/// Validated paths ready for the existing ELF, memory and timing importers.
#[derive(Clone, Debug)]
pub struct SessionInputs {
    pub label: String,
    pub elf_sha256: String,
    pub elf: PathBuf,
    pub map: Option<PathBuf>,
    pub automatic_map: bool,
    pub layout: Option<PathBuf>,
    pub automatic_layout: bool,
    pub trace: Option<PathBuf>,
    pub snapshots: Vec<PathBuf>,
    pub heap_journal: Option<PathBuf>,
}

#[derive(Deserialize)]
struct Manifest {
    schema_version: u32,
    build: Build,
    elf: String,
    map: Option<String>,
    #[serde(default)]
    automatic_map: bool,
    layout: Option<String>,
    #[serde(default)]
    automatic_layout: bool,
    trace: Option<String>,
    #[serde(default)]
    snapshots: Vec<String>,
    heap_journal: Option<String>,
    files: BTreeMap<String, FileIdentity>,
}

#[derive(Deserialize)]
struct Build {
    elf_sha256: String,
    #[serde(default)]
    label: String,
}

#[derive(Deserialize)]
struct FileIdentity {
    sha256: String,
    bytes: u64,
}

fn fail(message: impl Into<String>) -> Error {
    Error(format!("Capture session: {}", message.into()))
}

fn flat_name(name: &str) -> Result<()> {
    let mut parts = Path::new(name).components();
    if name.is_empty()
        || name.trim() != name
        || name
            .chars()
            .any(|ch| ch.is_control() || matches!(ch, '/' | '\\' | ':'))
        || !matches!(parts.next(), Some(Component::Normal(_)))
        || parts.next().is_some()
        || name == "session.json"
    {
        return Err(fail(format!(
            "expected a flat relative filename, got {name:?}"
        )));
    }
    Ok(())
}

fn valid_sha(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

fn regular_file(path: &Path, maximum: u64) -> Result<File> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.file_type().is_file() {
        return Err(fail(format!(
            "{} must be a regular file, without a symlink",
            path.display()
        )));
    }
    if metadata.len() == 0 || metadata.len() > maximum {
        return Err(fail(format!(
            "{} must contain 1..{maximum} bytes",
            path.display()
        )));
    }
    let file = File::open(path)?;
    if !file.metadata()?.is_file() || file.metadata()?.len() != metadata.len() {
        return Err(fail(format!("{} changed while opening", path.display())));
    }
    Ok(file)
}

fn bounded_reader(file: File, maximum: u64) -> Take<File> {
    // The extra byte detects a file that grows after its initial metadata check.
    file.take(maximum + 1)
}

/// Load a `session.json` file (or its containing directory), verify its inventory,
/// and resolve filenames without permitting a path to escape the session folder.
pub fn load_session(path: &Path) -> Result<SessionInputs> {
    let manifest_path = if path.is_dir() {
        path.join("session.json")
    } else {
        path.to_owned()
    };
    let mut bytes = Vec::new();
    bounded_reader(regular_file(&manifest_path, MAX_MANIFEST)?, MAX_MANIFEST)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_MANIFEST {
        return Err(fail("manifest exceeds 1 MiB"));
    }
    let manifest: Manifest = serde_json::from_slice(&bytes)
        .map_err(|error| fail(format!("invalid manifest: {error}")))?;
    if manifest.schema_version != 1 {
        return Err(fail("unsupported schema_version (expected 1)"));
    }
    if manifest.files.is_empty() || manifest.files.len() > MAX_FILES {
        return Err(fail("file inventory must contain 1..256 entries"));
    }
    if manifest.snapshots.len() > MAX_SNAPSHOTS {
        return Err(fail("at most 128 snapshot files are supported"));
    }
    let mut total = 0_u64;
    for (name, identity) in &manifest.files {
        flat_name(name)?;
        if !valid_sha(&identity.sha256) || identity.bytes == 0 || identity.bytes > 512 * MIB {
            return Err(fail(format!("invalid SHA-256 or byte count for {name}")));
        }
        total = total
            .checked_add(identity.bytes)
            .ok_or_else(|| fail("file-size overflow"))?;
        if total > MAX_SESSION {
            return Err(fail("combined file inventory exceeds 1 GiB"));
        }
    }
    let root = manifest_path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
        .canonicalize()?;
    let resolve = |name: &str, maximum: u64| -> Result<PathBuf> {
        flat_name(name)?;
        let identity = manifest
            .files
            .get(name)
            .ok_or_else(|| fail(format!("{name} is missing from files inventory")))?;
        if identity.bytes > maximum {
            return Err(fail(format!(
                "{name} exceeds its {maximum}-byte input limit"
            )));
        }
        Ok(root.join(name))
    };
    let optional = |name: &Option<String>, maximum| {
        name.as_deref()
            .map(|name| resolve(name, maximum))
            .transpose()
    };
    let result = SessionInputs {
        elf: resolve(&manifest.elf, 512 * MIB)?,
        map: optional(&manifest.map, 64 * MIB)?,
        automatic_map: manifest.automatic_map,
        layout: optional(&manifest.layout, 8 * MIB)?,
        automatic_layout: manifest.automatic_layout,
        trace: optional(&manifest.trace, 128 * MIB)?,
        snapshots: manifest
            .snapshots
            .iter()
            .map(|name| resolve(name, 32 * MIB))
            .collect::<Result<_>>()?,
        heap_journal: optional(&manifest.heap_journal, 32 * MIB)?,
        label: manifest.build.label,
        elf_sha256: manifest.build.elf_sha256,
    };
    if !valid_sha(&result.elf_sha256)
        || !result
            .elf_sha256
            .eq_ignore_ascii_case(&manifest.files[&manifest.elf].sha256)
    {
        return Err(fail(
            "build.elf_sha256 does not match the retained ELF identity",
        ));
    }
    let mut buffer = [0_u8; 64 * 1024];
    for (name, identity) in &manifest.files {
        let mut input = bounded_reader(
            regular_file(&root.join(name), identity.bytes)?,
            identity.bytes,
        );
        let mut digest = Sha256::new();
        let mut count = 0;
        loop {
            let read = input.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            count += read as u64;
            digest.update(&buffer[..read]);
        }
        if count != identity.bytes
            || !format!("{:x}", digest.finalize()).eq_ignore_ascii_case(&identity.sha256)
        {
            return Err(fail(format!(
                "{name} failed its SHA-256/byte-count integrity check"
            )));
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT: AtomicUsize = AtomicUsize::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "nxrs-session-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn manifest(&self) -> Value {
            let bytes = b"retained ELF bytes";
            fs::write(self.0.join("firmware.elf"), bytes).unwrap();
            let digest = format!("{:x}", Sha256::digest(bytes));
            json!({"schema_version":1, "build":{"elf_sha256":digest,"label":"fixture"},
                "elf":"firmware.elf", "files":{"firmware.elf":{"sha256":digest,"bytes":bytes.len()}}})
        }
        fn write(&self, manifest: &Value) {
            fs::write(
                self.0.join("session.json"),
                serde_json::to_vec(manifest).unwrap(),
            )
            .unwrap();
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn validates_retained_files_and_resolves_from_manifest_directory() {
        let fixture = Fixture::new();
        fixture.write(&fixture.manifest());
        let input = load_session(&fixture.0).unwrap();
        assert_eq!(input.label, "fixture");
        assert_eq!(
            input.elf,
            fixture.0.join("firmware.elf").canonicalize().unwrap()
        );
        assert!(input.trace.is_none());
        assert_eq!(
            load_session(&fixture.0.join("session.json")).unwrap().elf,
            input.elf
        );
        fs::write(&input.elf, b"changed ELF bytes!").unwrap();
        assert!(load_session(&fixture.0)
            .unwrap_err()
            .to_string()
            .contains("integrity"));
    }

    #[test]
    fn rejects_path_escape_missing_inventory_and_wrong_build() {
        let fixture = Fixture::new();
        let original = fixture.manifest();
        for (field, value, expected) in [
            ("elf", json!("../firmware.elf"), "flat relative filename"),
            ("elf", json!("firmware.elf "), "flat relative filename"),
            (
                "trace",
                json!("missing.json"),
                "missing from files inventory",
            ),
            (
                "build",
                json!({"elf_sha256":"00".repeat(32)}),
                "does not match",
            ),
            ("schema_version", json!(2), "unsupported schema_version"),
        ] {
            let mut manifest = original.clone();
            manifest[field] = value;
            fixture.write(&manifest);
            let error = load_session(&fixture.0).unwrap_err().to_string();
            assert!(error.contains(expected), "{error}");
        }
    }

    #[test]
    fn checks_unreferenced_retained_evidence_and_finite_limits() {
        let fixture = Fixture::new();
        let mut manifest = fixture.manifest();
        manifest["files"]["console.txt"] = json!({"sha256":"11".repeat(32),"bytes":4});
        fixture.write(&manifest);
        assert!(load_session(&fixture.0).is_err());
        fs::write(fixture.0.join("console.txt"), b"test").unwrap();
        assert!(load_session(&fixture.0)
            .unwrap_err()
            .to_string()
            .contains("console.txt"));
        manifest["files"]["console.txt"]["bytes"] = json!(u64::MAX);
        fixture.write(&manifest);
        assert!(load_session(&fixture.0)
            .unwrap_err()
            .to_string()
            .contains("byte count"));
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlinked_bundle_files() {
        let fixture = Fixture::new();
        let manifest = fixture.manifest();
        fixture.write(&manifest);
        fs::rename(
            fixture.0.join("firmware.elf"),
            fixture.0.join("elsewhere.elf"),
        )
        .unwrap();
        std::os::unix::fs::symlink("elsewhere.elf", fixture.0.join("firmware.elf")).unwrap();
        assert!(load_session(&fixture.0)
            .unwrap_err()
            .to_string()
            .contains("symlink"));
    }
}
