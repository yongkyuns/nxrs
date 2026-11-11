//! Prepare a private SDK using pinned inputs and reviewed source patches.
use crate::{archive, hash, output, read, source, write_json, Result};
use serde_json::json;
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
const LIBRARY: &str = "lib/rustlib/src/rust/library";
const CHANNEL_BRIDGE: &str = "lib/rustlib/src/rust/library/std/src/sys/sync/mpmc/nuttx/ffi/lock.c";
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
    let (unix, socket, version, checksum, parker_selector, selector_patch) = match sdk {
        "esp-1.90.0.0" => (
            "ba9e14b8009cd918cd9681dabe518ebf3c3a0a3a",
            "cc111f3521bc1516db00b69c510436060da844f8",
            "0.2.174",
            "1171693293099992e19cddea4e8b849964e9846f4acee11b3948bcc337be8776",
            "f4d8fa0a58c11b3a97c2492462119f94726a2e9a",
            "0008-esp-nuttx-parker-selection.patch",
        ),
        "nightly-2025-09-15" => (
            "dd1059fe04a2daf0b4ce610f380813417642f48f",
            "8216f8d2fd58294cf777fffbf41951869695fc22",
            "0.2.175",
            "6a82ae493e598baaea5209805c49bbf2ea7de956d50d7da0da1164f9c6d28543",
            "b9fb27b4eef2d48a7c8a556a2008f07986b949b3",
            "0009-nightly-nuttx-parker-selection.patch",
        ),
        _ => return Err("unsupported NuttX Rust SDK".into()),
    };
    let files = [
        ("std/src/rt.rs", "b3f3b301e3db6ff8596b8e637ebad49e22b44515"),
        ("std/src/sys/sync/thread_parking/mod.rs", parker_selector),
        (
            "std/src/sync/mpmc/mod.rs",
            "673033034eff5cb4b657dcfd7ddc2ae93b26da0a",
        ),
        (
            "std/src/sync/mpmc/list.rs",
            "050f26b097a0ef14ecbe26ca1b72f461f55dfe91",
        ),
        (
            "std/src/sync/mpmc/zero.rs",
            "f1ecf80fcb9f6f65e29bcb21529b8847f43a8139",
        ),
        ("std/src/sys/pal/unix/mod.rs", unix),
        ("std/src/sys/net/connection/socket/unix.rs", socket),
    ];
    for (path, expected) in files {
        expect(&source.join(LIBRARY).join(path), expected)?;
    }
    let created = [
        (
            "std/src/sys/sync/thread_parking/nuttx.rs",
            "std-parker.patch",
        ),
        (
            "std/src/sys/sync/mpmc/nuttx/mod.rs",
            "std-bounded-channel.patch",
        ),
        (
            "std/src/sys/sync/mpmc/nuttx/ffi/lock.c",
            "std-bounded-channel.patch",
        ),
    ];
    for (path, _) in created {
        if source.join(LIBRARY).join(path).symlink_metadata().is_ok() {
            return Err(format!("unexpected existing SDK native source: {path}"));
        }
    }
    fs::create_dir_all(out).map_err(|e| e.to_string())?;
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
            "std/src/rt.rs" => vec![(
                "0012-nuttx-main-thread-once.patch",
                "std-main-thread-once.patch",
            )],
            "std/src/sys/sync/thread_parking/mod.rs" => vec![
                ("0002-nuttx-parker.patch", "std-parker.patch"),
                (selector_patch, "std-parker-selection.patch"),
            ],
            "std/src/sync/mpmc/mod.rs" => vec![(
                "0007-nuttx-bounded-channel.patch",
                "std-bounded-channel.patch",
            )],
            "std/src/sync/mpmc/list.rs" => vec![(
                "0010-unbounded-operation-boundaries.patch",
                "std-unbounded-boundaries.patch",
            )],
            "std/src/sync/mpmc/zero.rs" => vec![(
                "0011-rendezvous-operation-boundaries.patch",
                "std-rendezvous-boundaries.patch",
            )],
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
    for (path, patch) in created {
        records.push(json!({"source_path":format!("{LIBRARY}/{path}"),"original_blob":null,"patched_blob":blob(&library.join(path))?,"patches":[patch]}));
    }
    let native_sources =
        [json!({"path":CHANNEL_BRIDGE,"sha256":hash(&destination.join(CHANNEL_BRIDGE))?})];
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
        &root
            .join("upstream/rust-std/patches")
            .join(if sdk == "esp-1.90.0.0" {
                "0005-nuttx-libc-override.patch"
            } else {
                "0006-nightly-nuttx-libc-override.patch"
            }),
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
        &json!({"mode":"nuttx-parker-fcntl-sigign-and-libc-socket-align","sdk":sdk,"files":records,"main_thread_initialization":"nuttx-image-once","bounded_channel_backend":"native-transaction-v1","native_sources":native_sources,"libc":{"libc_version":version,"crate_sha256":checksum,"source_blob":original_blob,"patched_blob":blob(&nuttx)?,"alignment":8,"open_flags_abi":"nuttx-13.1","path":format!("{LIBRARY}/libc-nuttx"),"pollfd_abi":"nuttx-13.1","directory_abi":"nuttx-13.1","patches":patches,"poll_sources":[{"path":"src/unix/mod.rs","original_blob":original_unix,"patched_blob":blob(&unix)?},{"path":"src/unix/nuttx/mod.rs","original_blob":flags_blob,"patched_blob":poll_blob}],"directory_sources":[{"path":"src/unix/nuttx/mod.rs","original_blob":poll_blob,"patched_blob":blob(&nuttx)?}]},"patchset_sha256":patchset,"startup_sig_ign":0,"libc_sockaddr_storage_alignment":8,"installed_toolchain_unchanged":true}),
    )?;
    Ok(destination)
}
/// SDK-owned header bindings join the existing native helper build automatically.
/// Cached older SDKs have no native additions; a new backend requires its ledger.
pub fn native_sources(sysroot: &Path, receipt: &Path) -> Result<Vec<PathBuf>> {
    let ledger = crate::read_json(receipt)?;
    let Some(entries) = ledger.get("native_sources") else {
        if sysroot.join(CHANNEL_BRIDGE).exists() {
            return Err("native SDK binding is missing its source ledger".into());
        }
        return Ok(vec![]);
    };
    let entries = entries
        .as_array()
        .ok_or("SDK native_sources must be an array")?;
    let root = sysroot.canonicalize().map_err(|e| e.to_string())?;
    let mut sources = vec![];
    for entry in entries {
        let path = Path::new(
            entry["path"]
                .as_str()
                .ok_or("SDK native source path missing")?,
        );
        if path
            .components()
            .any(|c| !matches!(c, std::path::Component::Normal(_)))
        {
            return Err("SDK native source path must stay relative to its sysroot".into());
        }
        let source = root.join(path).canonicalize().map_err(|e| e.to_string())?;
        if !source.starts_with(&root) || source.extension().is_none_or(|s| s != "c") {
            return Err("SDK native source escaped its sysroot or is not C".into());
        }
        if entry["sha256"].as_str() != Some(hash(&source)?.as_str()) {
            return Err("SDK native source digest mismatch".into());
        }
        sources.push(source);
    }
    if sysroot.join(CHANNEL_BRIDGE).exists()
        && !sources.contains(
            &sysroot
                .join(CHANNEL_BRIDGE)
                .canonicalize()
                .map_err(|e| e.to_string())?,
        )
    {
        return Err("SDK channel binding is missing from its source ledger".into());
    }
    Ok(sources)
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

    #[test]
    fn nuttx_main_identity_is_once_but_group_initialization_is_per_entry() {
        // Apply the real patch, then compile its actual init function with
        // observable OS/main-ID dependencies. The old source is a negative
        // control; this is not a claim of target ABI qualification.
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let runtime = temp.0.join(LIBRARY).join("std/src/rt.rs");
        fs::create_dir_all(runtime.parent().unwrap()).unwrap();
        let original = r#"unsafe fn init(argc: isize, argv: *const *const u8, sigpipe: u8) {
    #[cfg_attr(target_os = "teeos", allow(unused_unsafe))]
    unsafe {
        sys::init(argc, argv, sigpipe)
    };

    // Remember the main thread ID to give it the correct name.
    // SAFETY: this is the only time and place where we call this function.
    unsafe { main_thread::set(thread::current_id()) };
}

/// Clean up the thread-local runtime state. This *should* be run after all other
"#;
        fs::write(&runtime, original).unwrap();
        let patch = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../upstream/rust-std/patches/0012-nuttx-main-thread-once.patch");
        apply_dependency_patch(&temp.0, &patch, &temp.0, "std-main-thread-once.patch").unwrap();
        assert_eq!(
            hash(&patch).unwrap(),
            hash(&temp.0.join("std-main-thread-once.patch")).unwrap()
        );
        assert!(source::patch(&temp.0, &patch).is_err());
        let patched = read(&runtime).unwrap();
        let function = regex::Regex::new(r"(?ms)^unsafe fn init\(.*?^\}").unwrap();
        let dependencies = r#"
use std::sync::Once;
mod thread {
    pub fn current_id() -> std::thread::ThreadId { std::thread::current().id() }
}
mod main_thread {
    use std::sync::{Mutex, atomic::{AtomicUsize, Ordering}};
    pub static CALLS: AtomicUsize = AtomicUsize::new(0);
    pub static ID: Mutex<Option<std::thread::ThreadId>> = Mutex::new(None);
    pub unsafe fn set(id: std::thread::ThreadId) {
        CALLS.fetch_add(1, Ordering::Relaxed);
        *ID.lock().unwrap() = Some(id);
    }
}
mod sys {
    use std::sync::atomic::{AtomicUsize, Ordering};
    pub static CALLS: AtomicUsize = AtomicUsize::new(0);
    pub unsafe fn init(argc: isize, argv: *const *const u8, sigpipe: u8) {
        assert_eq!(argc, 0);
        assert!(argv.is_null());
        assert_eq!(sigpipe, 3);
        CALLS.fetch_add(1, Ordering::Relaxed);
    }
}
fn main() {
    use std::sync::atomic::Ordering;
    unsafe { init(0, std::ptr::null(), 3) };
    let first = *main_thread::ID.lock().unwrap();
    let mut entries = 1;
    if cfg!(target_os = "nuttx") {
        std::thread::scope(|scope| {
            for _ in 0..8 {
                scope.spawn(|| {
                    for _ in 0..16 {
                        unsafe { init(0, std::ptr::null(), 3) };
                    }
                });
            }
        });
        entries += 128;
    }
    assert_eq!(sys::CALLS.load(Ordering::Relaxed), entries,
               "OS initialization must run for every builtin entry");
    assert_eq!(main_thread::CALLS.load(Ordering::Relaxed), 1,
               "image-wide main ID must be published exactly once");
    assert_eq!(*main_thread::ID.lock().unwrap(), first);
}
"#;
        let rustc = std::env::var_os("RUSTC").unwrap_or_else(|| "rustc".into());
        for (name, runtime, nuttx, expected) in [
            ("nuttx", patched.as_str(), true, true),
            ("host", patched.as_str(), false, true),
            ("regression", original, true, false),
        ] {
            let input = temp.0.join(format!("{name}.rs"));
            let binary = temp
                .0
                .join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
            fs::write(
                &input,
                format!(
                    "{dependencies}\n{}\n",
                    function.find(runtime).unwrap().as_str()
                ),
            )
            .unwrap();
            let mut compiler = Command::new(&rustc);
            compiler.arg(&input).args([
                "--edition=2021",
                "-Aunused_imports",
                "-Aexplicit_builtin_cfgs_in_flags",
            ]);
            if nuttx {
                compiler.args(["--cfg", "target_os=\"nuttx\""]);
            }
            crate::run(compiler.arg("-o").arg(&binary)).unwrap();
            let result = Command::new(&binary).output().unwrap();
            assert_eq!(
                result.status.success(),
                expected,
                "{name}: {}",
                String::from_utf8_lossy(&result.stderr)
            );
            if !expected {
                assert!(String::from_utf8_lossy(&result.stderr)
                    .contains("image-wide main ID must be published exactly once"));
            }
        }
    }

    #[test]
    fn channel_outlining_is_selected_only_for_nuttx() {
        // Compile the actual patch annotations, not a rebuilt std. no_core lets
        // real target configurations select cfgs without foreign sysroots or
        // linkers; the original unconditional annotation is a regression control.
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let patches = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../upstream/rust-std/patches");
        let annotation =
            regex::Regex::new(r"(?m)^\+\s+(#\[[^\n]+\])\n +pub\(crate\) fn (\w+)\(").unwrap();
        let mut source = String::from(
            r#"#![feature(no_core, lang_items)]
#![allow(internal_features)]
#![no_core]
#[lang = "pointee_sized"] pub trait PointeeSized {}
#[lang = "meta_sized"] pub trait MetaSized: PointeeSized {}
#[lang = "sized"] pub trait Sized: MetaSized {}
#[no_mangle]
#[inline(never)]
pub extern "C" fn unconditional_control() {}
#[no_mangle]
pub extern "C" fn upstream_control() {}
"#,
        );
        let mut symbols = vec![];
        for (flavor, patch) in [
            ("list", "0010-unbounded-operation-boundaries.patch"),
            ("zero", "0011-rendezvous-operation-boundaries.patch"),
        ] {
            let text = read(&patches.join(patch)).unwrap();
            let operations = annotation.captures_iter(&text).collect::<Vec<_>>();
            assert_eq!(operations.len(), 4, "{patch}: expected four operations");
            for operation in operations {
                assert!(["try_send", "send", "try_recv", "recv"].contains(&&operation[2]));
                let symbol = format!("{flavor}_{}", &operation[2]);
                source.push_str(&format!(
                    "{}\n#[no_mangle]\npub extern \"C\" fn {symbol}() {{}}\n",
                    &operation[1]
                ));
                symbols.push(symbol);
            }
        }
        let input = temp.0.join("channel_scope.rs");
        fs::write(&input, source).unwrap();
        let definitions = regex::Regex::new(r"(?m)^define [^\n]*@(\w+)\([^\n]* #(\d+) \{").unwrap();
        let attributes = regex::Regex::new(r"(?m)^attributes #(\d+) = \{([^\n]*)\}").unwrap();
        let rustc = std::env::var_os("RUSTC").unwrap_or_else(|| "rustc".into());
        for (target, nuttx) in [
            ("x86_64-unknown-linux-gnu", false),
            ("x86_64-apple-darwin", false),
            ("x86_64-pc-windows-msvc", false),
            ("riscv32imac-unknown-nuttx-elf", true),
        ] {
            let ir = temp.0.join(format!("{target}.ll"));
            let result = Command::new(&rustc)
                .arg(&input)
                .args([
                    "--crate-type=lib",
                    "--edition=2021",
                    "--emit=llvm-ir",
                    "-Copt-level=0",
                    "--target",
                    target,
                    "-o",
                ])
                .arg(&ir)
                .env("RUSTC_BOOTSTRAP", "1")
                .output()
                .unwrap();
            assert!(
                result.status.success(),
                "{target}: {}",
                String::from_utf8_lossy(&result.stderr)
            );
            let ir = read(&ir).unwrap();
            let groups = attributes
                .captures_iter(&ir)
                .map(|capture| {
                    (
                        capture[1].to_owned(),
                        capture[2].split_ascii_whitespace().any(|s| s == "noinline"),
                    )
                })
                .collect::<std::collections::BTreeMap<_, _>>();
            let outlined = definitions
                .captures_iter(&ir)
                .map(|capture| (capture[1].to_owned(), groups[&capture[2]]))
                .collect::<std::collections::BTreeMap<_, _>>();
            assert_eq!(outlined.len(), 10, "{target}: missing compiler definitions");
            assert!(outlined["unconditional_control"], "{target}: old behavior");
            assert!(!outlined["upstream_control"], "{target}: upstream behavior");
            for symbol in &symbols {
                assert_eq!(outlined[symbol], nuttx, "{target}: {symbol}");
            }
        }
    }

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
    fn libc_override_accepts_both_supported_manifest_endings() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        crate::run(source::git(&temp.0).args(["init", "--quiet"])).unwrap();
        crate::run(source::git(&temp.0).args(["config", "core.autocrlf", "true"])).unwrap();
        let manifest = temp.0.join(LIBRARY).join("Cargo.toml");
        fs::create_dir_all(manifest.parent().unwrap()).unwrap();
        let patches = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../upstream/rust-std/patches");
        let anchor = "[patch.crates-io]\nrustc-std-workspace-core = { path = 'rustc-std-workspace-core' }\nrustc-std-workspace-alloc = { path = 'rustc-std-workspace-alloc' }\nrustc-std-workspace-std = { path = 'rustc-std-workspace-std' }\n";
        for (name, tail) in [
            ("0006-nightly-nuttx-libc-override.patch", ""),
            (
                "0005-nuttx-libc-override.patch",
                "compiler_builtins = { path = \"compiler-builtins/compiler-builtins\" }\n",
            ),
        ] {
            let patch = patches.join(name);
            fs::write(&manifest, format!("{anchor}{tail}")).unwrap();
            source::patch(&temp.0, &patch).unwrap();
            let text = read(&manifest).unwrap();
            assert!(text.contains("libc = { path = 'libc-nuttx' }"));
            assert!(text.ends_with(tail));
            assert!(source::patch(&temp.0, &patch).is_err());
        }
    }
    #[test]
    fn native_bindings_require_the_recorded_bytes_and_ledger() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let root = temp.0.join("sdk");
        fs::create_dir(&root).unwrap();
        let receipt = temp.0.join("std-patch.json");
        write_json(&receipt, &json!({})).unwrap();
        assert!(native_sources(&root, &receipt).unwrap().is_empty());
        let bridge = root.join(CHANNEL_BRIDGE);
        fs::create_dir_all(bridge.parent().unwrap()).unwrap();
        fs::write(&bridge, "/* native binding */").unwrap();
        assert!(native_sources(&root, &receipt).is_err());
        write_json(
            &receipt,
            &json!({"native_sources":[{"path":CHANNEL_BRIDGE,"sha256":hash(&bridge).unwrap()}]}),
        )
        .unwrap();
        assert_eq!(
            native_sources(&root, &receipt).unwrap(),
            [bridge.canonicalize().unwrap()]
        );
        fs::write(&bridge, "/* changed */").unwrap();
        assert!(native_sources(&root, &receipt)
            .unwrap_err()
            .contains("digest"));
    }
    #[test]
    fn native_binding_paths_cannot_escape_the_sdk() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let root = temp.0.join("sdk");
        fs::create_dir(&root).unwrap();
        let outside = temp.0.join("outside.c");
        fs::write(&outside, "/* outside */").unwrap();
        let receipt = temp.0.join("std-patch.json");
        for path in ["../outside.c".to_string(), outside.display().to_string()] {
            write_json(
                &receipt,
                &json!({"native_sources":[{"path":path,"sha256":hash(&outside).unwrap()}]}),
            )
            .unwrap();
            assert!(native_sources(&root, &receipt).is_err());
        }
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&outside, root.join("link.c")).unwrap();
            write_json(
                &receipt,
                &json!({"native_sources":[{"path":"link.c","sha256":hash(&outside).unwrap()}]}),
            )
            .unwrap();
            assert!(native_sources(&root, &receipt)
                .unwrap_err()
                .contains("escaped"));
        }
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
