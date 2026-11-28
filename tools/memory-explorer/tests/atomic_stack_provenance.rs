//! Host negative controls for native build admission; these do not execute ARM.

#[path = "support/atomic_stack.rs"]
mod atomic_stack;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

struct Evidence(PathBuf);

impl Evidence {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "nxrs-atomic-stack-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}

impl Drop for Evidence {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

fn sha256(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}

fn receipt(repo: &Path, builder: &str) -> Value {
    let mut hashes = serde_json::Map::new();
    for source in BTreeSet::from([atomic_stack::HELPER, builder]) {
        hashes.insert(source.into(), json!(sha256(&repo.join(source))));
    }
    json!({
        "atomic_stack_patch": {
            "path": atomic_stack::PATCH,
            "sha256": sha256(&repo.join(atomic_stack::PATCH)),
            "source": atomic_stack::SOURCE,
            "before": atomic_stack::BEFORE,
            "after": atomic_stack::AFTER,
            "source_restored": true
        },
        "build_sources_sha256": hashes
    })
}

#[test]
fn exact_retained_patch_and_each_builder_are_admitted() {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let evidence = Evidence::new();
    fs::copy(
        repo.join(atomic_stack::PATCH),
        evidence.0.join("atomic-stack.patch"),
    )
    .unwrap();
    for builder in [
        atomic_stack::HELPER,
        "tools/memory-explorer/tests/build_replay_nuttx.py",
        "tools/memory-explorer/tests/build_requests_nuttx.py",
        "tools/memory-explorer/tests/build_timed_capture_nuttx.py",
    ] {
        atomic_stack::validate(&receipt(&repo, builder), &evidence.0, &repo, builder);
    }
}

#[test]
fn missing_changed_or_unrestored_patch_receipts_are_rejected() {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let evidence = Evidence::new();
    fs::copy(
        repo.join(atomic_stack::PATCH),
        evidence.0.join("atomic-stack.patch"),
    )
    .unwrap();
    let valid = receipt(&repo, atomic_stack::HELPER);
    let reject = |build: &Value| {
        assert!(std::panic::catch_unwind(|| {
            atomic_stack::validate(build, &evidence.0, &repo, atomic_stack::HELPER);
        })
        .is_err());
    };
    for key in [
        "path",
        "sha256",
        "source",
        "before",
        "after",
        "source_restored",
    ] {
        let mut build = valid.clone();
        build["atomic_stack_patch"][key] = if key == "source_restored" {
            json!(false)
        } else {
            json!("different-source-identity")
        };
        reject(&build);
    }
    let mut build = valid.clone();
    build.as_object_mut().unwrap().remove("atomic_stack_patch");
    reject(&build);
    let mut build = valid.clone();
    build["atomic_stack_patch"]["unrecorded-field"] = json!(true);
    reject(&build);
    let mut build = valid.clone();
    build["build_sources_sha256"][atomic_stack::HELPER] = json!("0".repeat(64));
    reject(&build);
    let mut build = valid.clone();
    build["build_sources_sha256"] = json!({});
    reject(&build);
    let mut build = valid.clone();
    build["build_sources_sha256"]["unrecorded-helper.py"] = json!("0".repeat(64));
    reject(&build);
    fs::write(
        evidence.0.join("atomic-stack.patch"),
        b"different retained bytes\n",
    )
    .unwrap();
    reject(&valid);
}
