use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

pub const PATCH: &str = "upstream/nuttx/patches/0150-armv8m-atomic-stack-transition.patch";
pub const SOURCE: &str = "arch/arm/src/armv8-m/arm_initialstate.c";
pub const BEFORE: &str = "bad2478f501954bdf252ca71280b4025c5ed29ade912b4d94fc8f0957946520f";
pub const AFTER: &str = "f1a091dd7bf23825cdb5e9563cc37718a2c90b38014049919d0e7eb2d9528666";
pub const HELPER: &str = "tools/memory-explorer/tests/build_timing_nuttx.py";

fn sha256(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}

pub fn validate(build: &Value, build_dir: &Path, repo: &Path, builder: &str) {
    let patch = &build["atomic_stack_patch"];
    assert_eq!(patch.as_object().unwrap().len(), 6);
    assert_eq!(patch["path"], PATCH);
    assert_eq!(patch["source"], SOURCE);
    assert_eq!(patch["before"], BEFORE);
    assert_eq!(patch["after"], AFTER);
    assert_eq!(patch["source_restored"], true);
    let expected = sha256(&repo.join(PATCH));
    assert_eq!(patch["sha256"], expected);
    assert_eq!(sha256(&build_dir.join("atomic-stack.patch")), expected);
    let helpers = BTreeSet::from([HELPER, builder]);
    let hashes = build["build_sources_sha256"].as_object().unwrap();
    assert_eq!(hashes.len(), helpers.len());
    for source in helpers {
        assert_eq!(
            hashes[source],
            sha256(&repo.join(source)),
            "build helper differs"
        );
    }
}
