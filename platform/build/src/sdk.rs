//! Prepare a private SDK using pinned inputs and reviewed source patches.
use crate::{archive, hash, output, read, source, write_json, Result};
use serde_json::json;
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
const LIBRARY: &str = "lib/rustlib/src/rust/library";
fn blob(path: &Path) -> Result<String> {
    Ok(output(Command::new("git").arg("hash-object").arg(path))?
        .trim()
        .into())
}
fn expect(path: &Path, expected: &str) -> Result<()> {
    if blob(path)? != expected {
        return Err(format!(
            "unexpected SDK source; refuse drift/reapplication: {}",
            path.display()
        ));
    }
    Ok(())
}
pub fn apply_dependency_patch(source: &Path, patch: &Path, out: &Path, name: &str) -> Result<()> {
    source::patch(source, patch)?;
    fs::copy(patch, out.join(name)).map_err(|e| e.to_string())?;
    Ok(())
}
pub fn prepare(root: &Path, source: &Path, out: &Path, sdk: &str) -> Result<PathBuf> {
    let (unix, socket, version, checksum) = match sdk {
        "esp-1.90.0.0" => (
            "ba9e14b8009cd918cd9681dabe518ebf3c3a0a3a",
            "cc111f3521bc1516db00b69c510436060da844f8",
            "0.2.174",
            "1171693293099992e19cddea4e8b849964e9846f4acee11b3948bcc337be8776",
        ),
        "nightly-2025-09-15" => (
            "dd1059fe04a2daf0b4ce610f380813417642f48f",
            "8216f8d2fd58294cf777fffbf41951869695fc22",
            "0.2.175",
            "6a82ae493e598baaea5209805c49bbf2ea7de956d50d7da0da1164f9c6d28543",
        ),
        _ => return Err("unsupported NuttX Rust SDK".into()),
    };
    let files = [
        (
            "std/src/sys/sync/thread_parking/pthread.rs",
            "14bc793c15de254594778b59ad045006b58fd845",
        ),
        ("std/src/sys/pal/unix/mod.rs", unix),
        ("std/src/sys/net/connection/socket/unix.rs", socket),
    ];
    for (path, expected) in files {
        expect(&source.join(LIBRARY).join(path), expected)?;
    }
    let destination = out.join("toolchain");
    source::copy_tree(source, &destination)?;
    let library = destination.join(LIBRARY);
    if library
        .symlink_metadata()
        .map_err(|e| e.to_string())?
        .is_symlink()
    {
        fs::remove_file(&library).map_err(|e| e.to_string())?;
        source::copy_tree(
            &source
                .join(LIBRARY)
                .canonicalize()
                .map_err(|e| e.to_string())?,
            &library,
        )?;
    }
    let destination = destination.canonicalize().map_err(|e| e.to_string())?;
    if !library
        .canonicalize()
        .map_err(|e| e.to_string())?
        .starts_with(&destination)
    {
        return Err("SDK library escaped private copy".into());
    }
    let mut records = vec![];
    for (path, expected) in files {
        let selected = library.join(path);
        if !selected
            .canonicalize()
            .map_err(|e| e.to_string())?
            .starts_with(&destination)
        {
            return Err("SDK source symlink escaped private copy".into());
        }
        let patches = match path {
            "std/src/sys/sync/thread_parking/pthread.rs" => {
                vec![("0002-nuttx-parker.patch", "std-parker.patch")]
            }
            "std/src/sys/pal/unix/mod.rs" => vec![
                (
                    "0003-nuttx-fd-sanitization.patch",
                    "std-fd-sanitization.patch",
                ),
                ("0004-nuttx-sigign.patch", "std-sigign.patch"),
            ],
            _ => vec![(
                "0001-native-pollfd-initialization.patch",
                "std-pollfd-init.patch",
            )],
        };
        for (patch, name) in &patches {
            let base = if *name == "std-pollfd-init.patch" {
                &library
            } else {
                &destination
            };
            apply_dependency_patch(
                base,
                &root.join("upstream/rust-std/patches").join(patch),
                out,
                name,
            )?;
        }
        expect(&source.join(LIBRARY).join(path), expected)?;
        records.push(json!({"source_path":format!("{LIBRARY}/{path}"),"original_blob":expected,"patched_blob":blob(&selected)?,"patches":patches.iter().map(|(_,name)|name).collect::<Vec<_>>()}));
    }
    let cache = root
        .join("target/build-sources")
        .join(format!("libc-{version}.crate"));
    crate::fetch_bounded(
        &format!("https://static.crates.io/crates/libc/libc-{version}.crate"),
        checksum,
        2_000_000,
        None,
        &cache,
    )?;
    let unpack = library.join(format!("libc-{version}"));
    archive::compressed("gzip", &cache, &unpack, 10000, 128 * 1024 * 1024)?;
    let vendored = library.join("libc-nuttx");
    fs::rename(unpack, &vendored).map_err(|e| e.to_string())?;
    let nuttx = vendored.join("src/unix/nuttx/mod.rs");
    let original_blob = "69732d845b400e9b8781724d5a9dc3d5f3856913";
    expect(&nuttx, original_blob)?;
    let unix = vendored.join("src/unix/mod.rs");
    let original_unix = blob(&unix)?;
    let (mut flags_blob, mut poll_blob) = (String::new(), String::new());
    let mut patches = vec![];
    for (patch, name) in [
        (
            "0003-nuttx-sockaddr-alignment.patch",
            "libc-sockaddr-storage.patch",
        ),
        (
            "0004-nuttx-open-flags.patch",
            "libc-nuttx-13-open-flags.patch",
        ),
        ("0001-nuttx-pollfd-abi.patch", "libc-nuttx-poll.patch"),
        (
            "0002-nuttx-directory-inode-abi.patch",
            "libc-nuttx-directory.patch",
        ),
    ] {
        apply_dependency_patch(
            &vendored,
            &root.join("upstream/libc/patches").join(patch),
            out,
            name,
        )?;
        if name == "libc-nuttx-13-open-flags.patch" {
            flags_blob = blob(&nuttx)?;
        }
        if name == "libc-nuttx-poll.patch" {
            poll_blob = blob(&nuttx)?;
        }
        patches.push(name);
    }
    apply_dependency_patch(
        &destination,
        &root.join("upstream/rust-std/patches/0005-nuttx-libc-override.patch"),
        out,
        "std-libc-override.patch",
    )?;
    patches.push("std-libc-override.patch");
    let lock = library.join("Cargo.lock");
    let before = rewrite_lock(&destination, &lock, version)?;
    // Record a conventional, reviewable diff as well as the final source hashes.
    let original = out.join(".library-lock-original");
    fs::write(&original, &before).map_err(|e| e.to_string())?;
    let diff = Command::new("diff")
        .args([
            "-u",
            "--label",
            "a/lib/rustlib/src/rust/library/Cargo.lock",
            "--label",
            "b/lib/rustlib/src/rust/library/Cargo.lock",
        ])
        .arg(&original)
        .arg(&lock)
        .output()
        .map_err(|e| e.to_string())?;
    fs::remove_file(original).map_err(|e| e.to_string())?;
    if diff.status.code() != Some(1) {
        return Err("expected an explicit libc lockfile change".into());
    }
    fs::write(out.join("std-libc-lock.patch"), diff.stdout).map_err(|e| e.to_string())?;
    patches.push("std-libc-lock.patch");
    let mut patchset = serde_json::Map::new();
    for name in patches.iter().copied().chain(records.iter().flat_map(|r| {
        r["patches"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
    })) {
        patchset.insert(name.into(), json!(hash(&out.join(name))?));
    }
    write_json(
        &out.join("std-patch.json"),
        &json!({"mode":"nuttx-parker-fcntl-sigign-and-libc-socket-align","sdk":sdk,"files":records,"libc":{"libc_version":version,"crate_sha256":checksum,"source_blob":original_blob,"patched_blob":blob(&nuttx)?,"alignment":8,"open_flags_abi":"nuttx-13.1","path":format!("{LIBRARY}/libc-nuttx"),"pollfd_abi":"nuttx-13.1","directory_abi":"nuttx-13.1","patches":patches,"poll_sources":[{"path":"src/unix/mod.rs","original_blob":original_unix,"patched_blob":blob(&unix)?},{"path":"src/unix/nuttx/mod.rs","original_blob":flags_blob,"patched_blob":poll_blob}],"directory_sources":[{"path":"src/unix/nuttx/mod.rs","original_blob":poll_blob,"patched_blob":blob(&nuttx)?}]},"patchset_sha256":patchset,"startup_sig_ign":0,"libc_sockaddr_storage_alignment":8,"installed_toolchain_unchanged":true}),
    )?;
    Ok(destination)
}
fn rewrite_lock(private: &Path, lock: &Path, version: &str) -> Result<String> {
    if !lock
        .canonicalize()
        .map_err(|e| e.to_string())?
        .starts_with(private)
    {
        return Err("SDK lockfile symlink escaped private copy".into());
    }
    let before = read(lock)?;
    fs::write(lock, local_libc_lock(&before, version)?).map_err(|e| e.to_string())?;
    Ok(before)
}
fn local_libc_lock(text: &str, version: &str) -> Result<String> {
    let mut result = String::new();
    let mut found = 0;
    for (index, block) in text.split("[[package]]").enumerate() {
        if index != 0 {
            result.push_str("[[package]]");
        }
        if block.contains(&format!("name = \"libc\"\nversion = \"{version}\"\n")) {
            found += 1;
            for line in block.split_inclusive('\n') {
                if !line.starts_with("source = ") && !line.starts_with("checksum = ") {
                    result.push_str(line);
                }
            }
        } else {
            result.push_str(block);
        }
    }
    if found != 1 {
        return Err("expected exactly one locked libc package".into());
    }
    Ok(result)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    #[test]
    fn a_private_sdk_cannot_rewrite_an_external_lockfile() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let private = temp.0.join("private");
        fs::create_dir(&private).unwrap();
        let outside = temp.0.join("installed-Cargo.lock");
        let original =
            "[[package]]\nname = \"libc\"\nversion = \"0.2.175\"\nsource = \"registry\"\n";
        fs::write(&outside, original).unwrap();
        let lock = private.join("Cargo.lock");
        std::os::unix::fs::symlink(&outside, &lock).unwrap();
        assert!(rewrite_lock(&private, &lock, "0.2.175").is_err());
        assert_eq!(read(&outside).unwrap(), original);
    }
    #[test]
    fn locks_only_selected_libc() {
        let s="[[package]]\nname = \"libc\"\nversion = \"0.2.175\"\nsource = \"registry\"\nchecksum = \"x\"\n[[package]]\nname = \"other\"\nsource = \"keep\"\n";
        let fixed = local_libc_lock(s, "0.2.175").unwrap();
        assert!(!fixed.contains("checksum"));
        assert!(fixed.contains("source = \"keep\""));
        assert!(local_libc_lock(s, "0.2.174").is_err());
        assert!(local_libc_lock(&format!("{s}{s}"), "0.2.175").is_err());
    }
}
