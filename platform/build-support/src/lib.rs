//! Shared build-script support. No CLI, deployment, SDK provisioning or runtime code.
#![forbid(unsafe_code)]
pub use regex;
pub use serde_json as json;
pub mod archive;
pub mod composition;
pub mod native;
mod native_nuttx;
pub mod profile;
pub mod source;

use sha2::{Digest, Sha256};
use std::{fs, path::Path, process::Command};
pub type Result<T> = std::result::Result<T, String>;

pub fn run(command: &mut Command) -> Result<()> {
    let status = command.status().map_err(|e| format!("{command:?}: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("{command:?}: {status}"))
    }
}
pub fn output_bytes(command: &mut Command) -> Result<Vec<u8>> {
    let value = command.output().map_err(|e| format!("{command:?}: {e}"))?;
    if !value.status.success() {
        return Err(format!(
            "{command:?}: {}",
            String::from_utf8_lossy(&value.stderr)
        ));
    }
    Ok(value.stdout)
}
pub fn output(command: &mut Command) -> Result<String> {
    String::from_utf8(output_bytes(command)?).map_err(|e| e.to_string())
}
pub fn hash_bytes(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}
pub fn read(path: &Path) -> Result<String> {
    fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))
}
pub fn hash(path: &Path) -> Result<String> {
    use std::io::Read;
    let mut file = fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let n = file.read(&mut buffer).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
pub fn write_json(path: &Path, value: &serde_json::Value) -> Result<()> {
    let data = serde_json::to_string_pretty(value).map_err(|e| e.to_string())?;
    fs::write(path, data + "\n").map_err(|e| format!("{}: {e}", path.display()))
}

/// Resolve product profiles and pinned SDK inputs explicitly for external users.
/// In-tree builds may discover the checkout by its declared profile directories.
pub fn sdk_root(manifest: &Path) -> Result<std::path::PathBuf> {
    fn valid(root: &Path) -> bool {
        root.join("Cargo.toml").is_file()
            && root.join("platform/profiles").is_dir()
            && root.join("platform/nuttx/platforms").is_dir()
            && root.join("hal").is_dir()
    }
    println!("cargo:rerun-if-env-changed=NXRS_SDK_ROOT");
    if let Some(root) = std::env::var_os("NXRS_SDK_ROOT") {
        let root = std::path::PathBuf::from(root)
            .canonicalize()
            .map_err(|e| format!("NXRS_SDK_ROOT: {e}"))?;
        if valid(&root) {
            return Ok(root);
        }
        return Err("NXRS_SDK_ROOT must name a checkout with NXRS profiles and SDK inputs".into());
    }
    manifest
        .ancestors()
        .find(|root| valid(root))
        .ok_or("Set NXRS_SDK_ROOT to the product profiles and pinned SDK inputs")?
        .canonicalize()
        .map_err(|e| e.to_string())
}

/// Read the optional detector budget from the selected product profile.
pub fn detector_config(manifest: &Path) -> Result<Option<profile::Detector>> {
    println!("cargo:rerun-if-env-changed=NXRS_PLATFORM");
    let Some(name) = std::env::var_os("NXRS_PLATFORM") else {
        return Ok(None);
    };
    let name = name.to_str().ok_or("platform name must be UTF-8")?;
    let root = sdk_root(manifest)?;
    let profile = profile::Profile::resolve(&root, name)?;
    for path in &profile.sources {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    profile.detector()
}

pub fn configure_hal() -> Result<String> {
    use std::env;
    println!("cargo:rustc-check-cfg=cfg(hal_enabled)");
    println!("cargo:rustc-check-cfg=cfg(hal_mock)");
    println!("cargo:rerun-if-env-changed=NXRS_PLATFORM");
    println!("cargo:rerun-if-env-changed=NXRS_SDK_ROOT");
    for name in [
        "CARGO_CFG_HAL_ENABLED",
        "CARGO_CFG_HAL_MOCK",
        "CARGO_CFG_NXRS_HAL_PLATFORM",
        "CARGO_CFG_NXRS_PROVIDER",
    ] {
        if env::var_os(name).is_some() {
            return Err("Select one NXRS_PLATFORM, not hand-written provider cfg flags".into());
        }
    }
    let Some(name) = env::var_os("NXRS_PLATFORM") else {
        return Ok("none".into());
    };
    let name = name.to_str().ok_or("platform name must be UTF-8")?;
    let directory = std::path::PathBuf::from(
        env::var_os("CARGO_MANIFEST_DIR").ok_or("missing Cargo manifest directory")?,
    );
    let root = sdk_root(&directory)?;
    let profile = profile::Profile::resolve(&root, name)?;
    let package = env::var("CARGO_PKG_NAME").map_err(|e| e.to_string())?;
    let capability = package.strip_prefix("nxrs-").ok_or("HAL package name")?;
    let os = env::var("CARGO_CFG_TARGET_OS").map_err(|e| e.to_string())?;
    for line in profile.cargo_output(&root, capability, &os)? {
        println!("{line}")
    }
    let binding = profile.binding(capability);
    if ["macos", "linux", "nuttx"].contains(&binding) {
        let out = std::path::PathBuf::from(env::var_os("OUT_DIR").ok_or("missing OUT_DIR")?);
        let target = env::var("TARGET").map_err(|e| e.to_string())?;
        for line in native::build(&directory, &os, &target, &out)? {
            println!("{line}")
        }
    }
    Ok(binding.into())
}

pub fn read_json(path: &Path) -> Result<serde_json::Value> {
    serde_json::from_str(&read(path)?).map_err(|e| format!("{}: {e}", path.display()))
}
pub fn find_program(value: &str) -> Result<std::path::PathBuf> {
    find_program_on_path(value, &std::env::var_os("PATH").unwrap_or_default())
}
pub fn find_program_on_path(value: &str, path: &std::ffi::OsStr) -> Result<std::path::PathBuf> {
    let p = Path::new(value);
    if p.components().count() > 1 {
        return p.canonicalize().map_err(|e| format!("{value}: {e}"));
    }
    let program = std::env::split_paths(path)
        .map(|p| p.join(value))
        .find(|p| p.is_file())
        .ok_or_else(|| format!("missing prerequisite {value}"))?;
    std::path::absolute(program).map_err(|e| e.to_string())
}

pub fn split_flags(value: &str) -> Result<Vec<String>> {
    shlex::split(value).ok_or_else(|| "invalid compiler flags".into())
}
pub fn shell_quote(value: &str) -> String {
    shlex::try_quote(value)
        .expect("validated command argument contains no NUL")
        .into_owned()
}

/// Fetch one hash-pinned source file. Build scripts only consume preflighted caches.
pub fn fetch_verified(url: &str, expected: &str, size: u64, path: &Path) -> Result<()> {
    fetch_bounded(url, expected, size, Some(size), path)
}
pub fn fetch_bounded(
    url: &str,
    expected: &str,
    bound: u64,
    exact_size: Option<u64>,
    path: &Path,
) -> Result<()> {
    if path.exists() {
        let length = path.metadata().map_err(|e| e.to_string())?.len();
        if length <= bound
            && exact_size.is_none_or(|size| length == size)
            && hash(path)? == expected
        {
            return Ok(());
        }
        return Err(format!(
            "existing source cache failed verification: {}",
            path.display()
        ));
    }
    if !url.starts_with("https://")
        || expected.len() != 64
        || !expected.bytes().all(|b| b.is_ascii_hexdigit())
        || bound == 0
    {
        return Err("invalid pinned source descriptor".into());
    }
    let parent = path.parent().ok_or("source cache requires a parent")?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let temporary = TempDir::new(parent)?;
    let fresh = temporary.0.join("source");
    let result = (|| {
        run(Command::new("curl")
            .args([
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "20",
                "--max-time",
                "120",
                "--retry",
                "2",
                "--max-filesize",
            ])
            .arg(bound.to_string())
            .arg("--output")
            .arg(&fresh)
            .arg(url))?;
        let length = fresh.metadata().map_err(|e| e.to_string())?.len();
        if length > bound
            || exact_size.is_some_and(|size| length != size)
            || hash(&fresh)? != expected
        {
            return Err("download size/checksum mismatch".into());
        }
        fs::rename(&fresh, path).map_err(|e| e.to_string())
    })();
    let _ = fs::remove_file(&fresh);
    result
}

/// Ignore only absence when removing generated build state.
pub fn remove_file(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}
pub fn remove_dir(path: &Path) -> Result<()> {
    match fs::remove_dir_all(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}
pub struct TempDir(pub std::path::PathBuf);
impl TempDir {
    pub fn new(parent: &Path) -> Result<Self> {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        loop {
            let path = parent.join(format!(
                ".build-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&path) {
                Ok(()) => {
                    let dir = Self(path);
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        fs::set_permissions(&dir.0, fs::Permissions::from_mode(0o700))
                            .map_err(|e| e.to_string())?;
                    }
                    return Ok(dir);
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e.to_string()),
            }
        }
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
mod source_cache_tests {
    use super::*;
    #[test]
    fn cache_hits_still_verify_size_and_digest() {
        let temp = TempDir::new(&std::env::temp_dir()).unwrap();
        let path = temp.0.join("asset");
        fs::write(&path, b"pinned").unwrap();
        let digest = hash_bytes(b"pinned");
        fetch_bounded("https://unused.invalid", &digest, 6, Some(6), &path).unwrap();
        for (expected, bound, size) in [
            (digest.as_str(), 5, None),
            (digest.as_str(), 8, Some(7)),
            (
                "0000000000000000000000000000000000000000000000000000000000000000",
                6,
                Some(6),
            ),
        ] {
            assert!(fetch_bounded("https://unused.invalid", expected, bound, size, &path).is_err());
            assert_eq!(fs::read(&path).unwrap(), b"pinned");
        }
    }
}
