#![cfg_attr(
    not(all(target_os = "linux", target_arch = "x86_64")),
    allow(dead_code)
)]

use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

type Result<T> = std::result::Result<T, &'static str>;
const LIB: &str = "/usr/lib/x86_64-linux-gnu";
const LOADER: &str = "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2";
const SOURCE_POLICY: &[u8] = include_bytes!("../../../security/gateway-proxy-dependencies.json");

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
struct Libraries {
    loader: String,
    libc: String,
    crypto: String,
    ssl: String,
}
impl Libraries {
    fn files(&self) -> [(&str, &str); 4] {
        [
            (LOADER, &self.loader),
            ("/usr/lib/x86_64-linux-gnu/libc.so.6", &self.libc),
            ("/usr/lib/x86_64-linux-gnu/libcrypto.so.3", &self.crypto),
            ("/usr/lib/x86_64-linux-gnu/libssl.so.3", &self.ssl),
        ]
    }
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub(super) struct Policy {
    schema: u32,
    scope: String,
    source_policy_sha256: String,
    pub(super) qualification_sha256: String,
    reviewed_at: u64,
    valid_until: u64,
    pub(super) binary_sha256: String,
    libraries: Libraries,
}

fn require(value: bool, category: &'static str) -> Result<()> {
    if value {
        Ok(())
    } else {
        Err(category)
    }
}
pub(super) fn sha(raw: &[u8]) -> String {
    format!("{:x}", Sha256::digest(raw))
}
pub(super) fn hash_text(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub(super) fn policy(raw: &[u8], now: u64) -> Result<Policy> {
    require(raw.len() <= 8192, "policy-budget")?;
    let value: Policy = serde_json::from_slice(raw).map_err(|_| "policy-schema")?;
    require(
        value.schema == 1 && value.scope == "operator-approved-minimal-camera-proxy",
        "policy-scope",
    )?;
    require(
        value.source_policy_sha256 == sha(SOURCE_POLICY),
        "source-policy",
    )?;
    for h in [
        value.binary_sha256.as_str(),
        value.qualification_sha256.as_str(),
    ]
    .into_iter()
    .chain(value.libraries.files().into_iter().map(|(_, h)| h))
    {
        require(hash_text(h), "policy-digest")?;
    }
    require(
        value.reviewed_at <= now
            && now < value.valid_until
            && value
                .valid_until
                .checked_sub(value.reviewed_at)
                .is_some_and(|n| n > 0 && n <= 31 * 86400),
        "policy-expiry",
    )?;
    // An operator may shorten the publisher advisory review; cannot extend it.
    let source: serde_json::Value =
        serde_json::from_slice(SOURCE_POLICY).map_err(|_| "source-policy")?;
    require(
        source["advisory_review"]["reviewed_at"].as_str().is_some(),
        "source-policy",
    )?;
    // Exact bounds are copied by staging and independently bound to the compiled policy.
    let review: ReviewBounds =
        serde_json::from_value(source["advisory_review"].clone()).map_err(|_| "source-policy")?;
    require(
        value.reviewed_at == review.reviewed_at_unix
            && value.valid_until <= review.valid_until_unix,
        "source-review-window",
    )?;
    Ok(value)
}

// The source policy also carries canonical Unix bounds, avoiding a runtime date library.
#[derive(Deserialize)]
struct ReviewBounds {
    reviewed_at_unix: u64,
    valid_until_unix: u64,
}

pub(super) fn absolute(path: &Path) -> Result<()> {
    let s = path.to_str().ok_or("absolute-path")?;
    require(
        s.starts_with('/')
            && s.len() <= 4096
            && !s.ends_with('/')
            && s[1..].split('/').all(|part| {
                !part.is_empty()
                    && part != "."
                    && part != ".."
                    && part
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"_.+-".contains(&b))
            }),
        "absolute-path",
    )
}
fn root_directory(path: &Path) -> Result<()> {
    let m = std::fs::symlink_metadata(path).map_err(|_| "protected-parent")?;
    require(
        m.is_dir() && m.uid() == 0 && m.mode() & 0o022 == 0,
        "protected-parent",
    )
}
pub(super) fn parents(path: &Path) -> Result<()> {
    absolute(path)?;
    for parent in path.ancestors().skip(1) {
        root_directory(parent)?;
    }
    Ok(())
}
fn protected_metadata(m: &std::fs::Metadata, max: u64, executable: bool) -> Result<()> {
    require(m.is_file(), "file-type")?;
    require(m.uid() == 0, "file-owner")?;
    require(m.mode() & 0o022 == 0, "file-writable")?;
    require(m.nlink() == 1, "file-links")?;
    require(m.len() > 0 && m.len() <= max, "file-size")?;
    require(!executable || m.mode() & 0o111 != 0, "file-executable")?;
    require(m.mode() & 0o6000 == 0, "file-special-mode")
}
#[derive(PartialEq, Eq)]
pub(super) struct Identity {
    device: u64,
    inode: u64,
    links: u64,
    uid: u32,
    gid: u32,
    mode: u32,
    bytes: u64,
    modified: (i64, i64),
    changed: (i64, i64),
}
pub(super) fn identity(m: &std::fs::Metadata) -> Identity {
    Identity {
        device: m.dev(),
        inode: m.ino(),
        links: m.nlink(),
        uid: m.uid(),
        gid: m.gid(),
        mode: m.mode(),
        bytes: m.len(),
        modified: (m.mtime(), m.mtime_nsec()),
        changed: (m.ctime(), m.ctime_nsec()),
    }
}

#[derive(Clone, Copy)]
enum FileRole {
    Selection,
    Approval,
    Binary,
    Loader,
    Libc,
    Crypto,
    Ssl,
}
impl FileRole {
    fn name(self) -> &'static str {
        match self {
            Self::Selection => "selection",
            Self::Approval => "approval",
            Self::Binary => "binary",
            Self::Loader => "loader",
            Self::Libc => "libc",
            Self::Crypto => "crypto",
            Self::Ssl => "ssl",
        }
    }
}

// Fixed resource roles and operation categories only: never emit a path, OS
// error message, policy contents, or credentials when admission fails.
fn file(
    path: &Path,
    max: u64,
    executable: bool,
    collect: bool,
    role: FileRole,
) -> Result<(String, Vec<u8>)> {
    file_contents(path, max, executable, collect).map_err(|category| {
        eprintln!(
            "nxrs-gateway-proxy: file-refusal ({}: {})",
            role.name(),
            category
        );
        match category {
            "file-stat"
            | "file-type"
            | "file-owner"
            | "file-writable"
            | "file-links"
            | "file-size"
            | "file-executable"
            | "file-special-mode"
            | "file-open"
            | "file-descriptor-stat"
            | "file-descriptor-restat"
            | "file-restat" => "protected-file",
            _ => category,
        }
    })
}

fn file_contents(
    path: &Path,
    max: u64,
    executable: bool,
    collect: bool,
) -> Result<(String, Vec<u8>)> {
    parents(path)?;
    let before = std::fs::symlink_metadata(path).map_err(|_| "file-stat")?;
    protected_metadata(&before, max, executable)?;
    let mut f = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| "file-open")?;
    let opened = f.metadata().map_err(|_| "file-descriptor-stat")?;
    require(identity(&before) == identity(&opened), "file-replaced")?;
    let mut h = Sha256::new();
    let mut raw = Vec::new();
    let mut total = 0;
    let mut buffer = [0; 16384];
    loop {
        let n = f.read(&mut buffer).map_err(|_| "file-read")?;
        if n == 0 {
            break;
        }
        total += n as u64;
        require(total <= max, "file-budget")?;
        h.update(&buffer[..n]);
        if collect {
            raw.extend_from_slice(&buffer[..n]);
        }
    }
    let after = f.metadata().map_err(|_| "file-descriptor-restat")?;
    let named = std::fs::symlink_metadata(path).map_err(|_| "file-restat")?;
    require(
        identity(&opened) == identity(&after)
            && identity(&after) == identity(&named)
            && total == opened.len(),
        "file-changed",
    )?;
    Ok((format!("{:x}", h.finalize()), raw))
}
fn check_file(path: &Path, digest: &str, executable: bool, role: FileRole) -> Result<()> {
    require(
        file(path, 64 * 1024 * 1024, executable, false, role)?.0 == digest,
        "file-digest",
    )
}

fn loader() -> Command {
    let mut c = Command::new(LOADER);
    c.env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LANG", "C")
        .env("LC_ALL", "C")
        .env("OPENSSL_CONF", "/dev/null")
        .env("OPENSSL_MODULES", "/nonexistent")
        .args([
            "--inhibit-cache",
            "--library-path",
            LIB,
            "--glibc-hwcaps-mask",
            "",
        ]);
    // --list must precede the binary, added by the caller.
    c
}

fn resolved(raw: &[u8]) -> Result<BTreeSet<String>> {
    require(raw.len() <= 8192, "loader-budget")?;
    let text = std::str::from_utf8(raw).map_err(|_| "loader-output")?;
    let mut paths = BTreeSet::new();
    let mut vdso = false;
    for line in text.lines() {
        let parts: Vec<_> = line.split_whitespace().collect();
        let (path, address) = match parts.as_slice() {
            ["linux-vdso.so.1", address] if !vdso => {
                vdso = true;
                (None, *address)
            }
            [path, address] if *path == LOADER => (Some(*path), *address),
            [name, "=>", path, address]
                if Path::new(path).file_name().and_then(|n| n.to_str()) == Some(name)
                    || (*name == "/lib64/ld-linux-x86-64.so.2" && *path == LOADER) =>
            {
                (Some(*path), *address)
            }
            _ => return Err("loader-output"),
        };
        let hex = address
            .strip_prefix("(0x")
            .and_then(|s| s.strip_suffix(')'))
            .ok_or("loader-output")?;
        require(
            !hex.is_empty() && hex.len() <= 16 && hex.bytes().all(|c| c.is_ascii_hexdigit()),
            "loader-output",
        )?;
        if let Some(path) = path {
            require(paths.insert(path.to_owned()), "loader-duplicate")?;
        }
    }
    let expected = Libraries {
        loader: String::new(),
        libc: String::new(),
        crypto: String::new(),
        ssl: String::new(),
    }
    .files()
    .into_iter()
    .map(|(p, _)| p.to_owned())
    .collect();
    require(vdso && paths == expected, "loader-library-set")?;
    Ok(paths)
}

fn list(binary: &Path) -> Result<Vec<u8>> {
    let mut command = loader();
    command.arg("--list").arg(binary);
    bounded_list(&mut command)
}

fn bounded_list(command: &mut Command) -> Result<Vec<u8>> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "loader-start")?;
    let stdout = child.stdout.take().ok_or("loader-output")?;
    // The verified glibc loader does not fork. Retire it before joining its pipe reader.
    let reader = std::thread::spawn(move || {
        let mut raw = Vec::new();
        stdout.take(8193).read_to_end(&mut raw).map(|_| raw)
    });
    let deadline = Instant::now() + Duration::from_secs(3);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                break Err("loader-deadline");
            }
        }
    };
    let raw = reader
        .join()
        .map_err(|_| "loader-output")?
        .map_err(|_| "loader-output")?;
    require(raw.len() <= 8192, "loader-budget")?;
    require(status?.success(), "loader-status")?;
    Ok(raw)
}

#[derive(Debug)]
enum ArtifactSource {
    Policy(PathBuf),
    Selection(PathBuf),
}

// One regular-file selector names an immutable, digest-addressed policy/image
// directory. No symlink, arbitrary executable path or implicit fallback is used.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Selection {
    schema: u32,
    scope: String,
    approval_sha256: String,
}

pub(super) fn selected_policy(path: &Path, raw: &[u8]) -> Result<(PathBuf, String)> {
    absolute(path)?;
    require(raw.len() <= 1024, "selection-budget")?;
    let selected: Selection = serde_json::from_slice(raw).map_err(|_| "selection-schema")?;
    require(
        selected.schema == 1
            && selected.scope == "operator-approved-minimal-camera-proxy-selection"
            && hash_text(&selected.approval_sha256),
        "selection-scope",
    )?;
    let policy = path
        .parent()
        .ok_or("selection-parent")?
        .join("versions")
        .join(&selected.approval_sha256)
        .join("approval.json");
    absolute(&policy)?;
    Ok((policy, selected.approval_sha256))
}

pub(super) fn selection_bytes(digest: &str) -> Result<Vec<u8>> {
    require(hash_text(digest), "selection-scope")?;
    serde_json::to_vec(&serde_json::json!({"schema":1,
        "scope":"operator-approved-minimal-camera-proxy-selection","approval_sha256":digest}))
    .map_err(|_| "selection-schema")
}

pub(super) fn verify_pair_graph(binary: &Path, value: &Policy) -> Result<()> {
    check_file(binary, &value.binary_sha256, true, FileRole::Binary)?;
    for ((path, digest), role) in value.libraries.files().into_iter().zip([
        FileRole::Loader,
        FileRole::Libc,
        FileRole::Crypto,
        FileRole::Ssl,
    ]) {
        check_file(Path::new(path), digest, false, role)?;
    }
    match std::fs::symlink_metadata("/etc/ld.so.preload") {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        _ => return Err("system-preload"),
    }
    resolved(&list(binary)?)?;
    check_file(binary, &value.binary_sha256, true, FileRole::Binary)?;
    for ((path, digest), role) in value.libraries.files().into_iter().zip([
        FileRole::Loader,
        FileRole::Libc,
        FileRole::Crypto,
        FileRole::Ssl,
    ]) {
        check_file(Path::new(path), digest, false, role)?;
    }
    Ok(())
}

struct Arguments {
    artifact: ArtifactSource,
    config: PathBuf,
    runtime: PathBuf,
    check: bool,
}
fn arguments(args: impl Iterator<Item = String>) -> Result<Arguments> {
    let mut artifact = None;
    let mut config = None;
    let mut runtime = None;
    let mut check = false;
    let mut args = args;
    while let Some(arg) = args.next() {
        if arg == "--check" && !check {
            check = true;
            continue;
        }
        if matches!(arg.as_str(), "--policy" | "--selection") {
            require(artifact.is_none(), "arguments")?;
            let value = PathBuf::from(args.next().ok_or("arguments")?);
            absolute(&value)?;
            artifact = Some(if arg == "--policy" {
                ArtifactSource::Policy(value)
            } else {
                ArtifactSource::Selection(value)
            });
            continue;
        }
        let place = match arg.as_str() {
            "--config" => &mut config,
            "--runtime" => &mut runtime,
            _ => return Err("arguments"),
        };
        require(place.is_none(), "arguments")?;
        let value = PathBuf::from(args.next().ok_or("arguments")?);
        absolute(&value)?;
        *place = Some(value);
    }
    Ok(Arguments {
        artifact: artifact.ok_or("arguments")?,
        config: config.ok_or("arguments")?,
        runtime: runtime.ok_or("arguments")?,
        check,
    })
}

pub(super) fn run() -> Result<()> {
    let args = arguments(std::env::args().skip(1))?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "clock")?
        .as_secs();
    let (policy_path, approval_digest) = match &args.artifact {
        ArtifactSource::Policy(path) => (path.clone(), None),
        ArtifactSource::Selection(path) => {
            let raw = file(path, 1024, false, true, FileRole::Selection)?.1;
            let (selected, digest) = selected_policy(path, &raw)?;
            (selected, Some(digest))
        }
    };
    let (actual_digest, raw) = file(&policy_path, 8192, false, true, FileRole::Approval)?;
    require(
        approval_digest.is_none_or(|expected| expected == actual_digest),
        "selection-policy-digest",
    )?;
    let value = policy(&raw, now)?;
    let binary = policy_path.parent().ok_or("policy-parent")?.join("nginx");
    parents(&args.runtime)?;
    let m = std::fs::symlink_metadata(&args.runtime).map_err(|_| "runtime-directory")?;
    let uid = std::fs::metadata("/proc/self")
        .map_err(|_| "runtime-owner")?
        .uid();
    require(
        m.is_dir() && m.uid() == uid && m.mode() & 0o777 == 0o700,
        "runtime-directory",
    )?;
    verify_pair_graph(&binary, &value)?;
    let mut command = loader();
    command
        .arg(&binary)
        .args(["-e", "/dev/null", "-p"])
        .arg(&args.runtime)
        .arg("-c")
        .arg(&args.config)
        .stdin(Stdio::null());
    if args.check {
        command.arg("-t");
    }
    // Foreground nginx inherits the service PID; no resident helper or async runtime.
    let _ = command.exec();
    Err("proxy-exec")
}

#[cfg(test)]
mod tests {
    use super::*;
    fn valid() -> serde_json::Value {
        let source: serde_json::Value = serde_json::from_slice(SOURCE_POLICY).unwrap();
        serde_json::json!({"schema":1,"scope":"operator-approved-minimal-camera-proxy",
            "source_policy_sha256":sha(SOURCE_POLICY),"qualification_sha256":"a".repeat(64),
            "reviewed_at":source["advisory_review"]["reviewed_at_unix"],"valid_until":source["advisory_review"]["valid_until_unix"],
            "binary_sha256":"b".repeat(64),"libraries":{"loader":"c".repeat(64),"libc":"d".repeat(64),"crypto":"e".repeat(64),"ssl":"f".repeat(64)}})
    }
    #[test]
    fn policy_window_is_bounded_by_compiled_review() {
        let mut value = valid();
        let begin = value["reviewed_at"].as_u64().unwrap();
        let end = value["valid_until"].as_u64().unwrap();
        let read = |v: &serde_json::Value, now| policy(&serde_json::to_vec(v).unwrap(), now);
        assert!(read(&value, begin).is_ok());
        assert!(read(&value, begin - 1).is_err());
        assert!(read(&value, end).is_err());
        value["valid_until"] = (end + 1).into();
        assert!(read(&value, begin).is_err());
        value["valid_until"] = (begin + 31 * 86400 + 1).into();
        assert!(read(&value, begin).is_err());
    }
    #[test]
    fn duplicate_unknown_fields_wrong_sources_and_bad_digests_refuse() {
        let mut v = valid();
        let now = v["reviewed_at"].as_u64().unwrap();
        let raw = serde_json::to_string(&v).unwrap();
        for raw in [
            raw.replacen('{', "{\"schema\":1,", 1),
            raw.replacen('{', "{\"allow_any\":true,", 1),
            " ".repeat(8193),
        ] {
            assert!(policy(raw.as_bytes(), now).is_err());
        }
        v["source_policy_sha256"] = "0".repeat(64).into();
        assert!(policy(&serde_json::to_vec(&v).unwrap(), now).is_err());
        v = valid();
        v["libraries"]["ssl"] = "Z".repeat(64).into();
        assert!(policy(&serde_json::to_vec(&v).unwrap(), now).is_err());
        v = valid();
        v["libraries"]["extra"] = "a".repeat(64).into();
        assert!(policy(&serde_json::to_vec(&v).unwrap(), now).is_err());
    }
    fn listing() -> String {
        format!("linux-vdso.so.1 (0x123)\nlibcrypto.so.3 => {LIB}/libcrypto.so.3 (0x124)\nlibssl.so.3 => {LIB}/libssl.so.3 (0x125)\nlibc.so.6 => {LIB}/libc.so.6 (0x126)\n{LOADER} (0x127)\n")
    }
    #[test]
    fn loader_must_resolve_exact_reviewed_paths_without_duplicates() {
        let raw = listing();
        assert!(resolved(
            raw.replace(
                &format!("{LOADER} ("),
                &format!("/lib64/ld-linux-x86-64.so.2 => {LOADER} (")
            )
            .as_bytes()
        )
        .is_ok());
        assert_eq!(resolved(raw.as_bytes()).unwrap().len(), 4);
        for changed in [
            raw.replace("/libssl.so.3", "/glibc-hwcaps/x86-64-v3/libssl.so.3"),
            raw.replace("libc.so.6 =>", "other.so =>"),
            raw.replace("(0x126)", "not found"),
            raw.clone() + &format!("{LOADER} (0x128)\n"),
            raw.replace("linux-vdso.so.1 (0x123)\n", ""),
            raw + "libpcre.so => /usr/lib/libpcre.so (0x129)\n",
        ] {
            assert!(resolved(changed.as_bytes()).is_err());
        }
    }
    #[test]
    fn owned_loader_child() {
        match std::env::var("NXRS_PROXY_OWNED_CHILD").as_deref() {
            Ok("timeout") => std::thread::sleep(Duration::from_secs(30)),
            Ok("oversize") => {
                use std::io::Write;
                std::io::stdout().write_all(&[b'x'; 8193]).unwrap();
            }
            Ok("failure") => std::process::exit(3),
            _ => {}
        }
    }
    #[test]
    fn loader_deadline_and_output_failure_retire_owned_children() {
        for (kind, category) in [
            ("timeout", "loader-deadline"),
            ("oversize", "loader-budget"),
            ("failure", "loader-status"),
        ] {
            let mut child = Command::new(std::env::current_exe().unwrap());
            child
                .args([
                    "--exact",
                    "admission::tests::owned_loader_child",
                    "--nocapture",
                ])
                .env("NXRS_PROXY_OWNED_CHILD", kind);
            let before = Instant::now();
            assert_eq!(bounded_list(&mut child).unwrap_err(), category);
            assert!(before.elapsed() < Duration::from_secs(10));
        }
    }
    #[test]
    fn selector_names_one_digest_addressed_pair_and_refuses_unbound_paths() {
        let digest = "a".repeat(64);
        let value = serde_json::json!({"schema":1,
            "scope":"operator-approved-minimal-camera-proxy-selection", "approval_sha256":digest});
        let raw = serde_json::to_vec(&value).unwrap();
        let (path, actual) = selected_policy(Path::new("/owned/proxy/active.json"), &raw).unwrap();
        assert_eq!(actual, digest);
        assert_eq!(
            path,
            Path::new("/owned/proxy/versions")
                .join(&digest)
                .join("approval.json")
        );
        for changed in [
            serde_json::json!({"schema":1,"scope":"operator-approved-minimal-camera-proxy-selection","approval_sha256":"../outside"}),
            serde_json::json!({"schema":1,"scope":"operator-approved-minimal-camera-proxy-selection","approval_sha256":digest,"nginx":"/outside"}),
            serde_json::json!({"schema":true,"scope":"operator-approved-minimal-camera-proxy-selection","approval_sha256":digest}),
        ] {
            assert!(selected_policy(
                Path::new("/owned/proxy/active.json"),
                &serde_json::to_vec(&changed).unwrap()
            )
            .is_err());
        }
        let duplicate = String::from_utf8(raw)
            .unwrap()
            .replacen('{', "{\"schema\":1,", 1);
        assert!(
            selected_policy(Path::new("/owned/proxy/active.json"), duplicate.as_bytes()).is_err()
        );
        assert!(selected_policy(Path::new("/owned/proxy/active.json"), &[b' '; 1025]).is_err());
    }

    #[test]
    fn selection_is_explicit_and_cannot_fall_back_or_mix_with_another_source() {
        let base = [
            "--selection",
            "/owned/active.json",
            "--config",
            "/owned/config",
            "--runtime",
            "/owned/runtime",
        ];
        assert!(matches!(
            arguments(base.iter().map(|v| v.to_string()))
                .unwrap()
                .artifact,
            ArtifactSource::Selection(_)
        ));
        for extra in [
            ["--policy", "/owned/policy"],
            ["--selection", "/owned/other"],
        ] {
            assert!(arguments(base.iter().chain(extra.iter()).map(|v| v.to_string())).is_err());
        }
    }

    #[test]
    fn only_fixed_arguments_and_canonical_absolute_paths_are_accepted() {
        let base = [
            "--policy",
            "/usr/local/lib/nxrs-proxy/approval.json",
            "--config",
            "/run/credentials/proxy/config",
            "--runtime",
            "/run/nxrs-proxy",
        ];
        assert!(arguments(base.iter().map(|s| s.to_string())).is_ok());
        for extra in [
            vec!["--policy", "/tmp/other"],
            vec!["--check", "--check"],
            vec!["--nginx-arg", "-s"],
            vec!["--config"],
        ] {
            assert!(arguments(base.iter().chain(extra.iter()).map(|s| s.to_string())).is_err());
        }
        for path in [
            "relative", "/a/../b", "/a/./b", "/a//b", "/a/", "/a b", "/a\nb",
        ] {
            assert!(absolute(Path::new(path)).is_err());
        }
        assert!(file(
            Path::new("/tmp/absent-proxy-admission-test"),
            8192,
            false,
            false,
            FileRole::Approval
        )
        .is_err());
    }
}
