//! Shared build-script support. No CLI, deployment, SDK provisioning or runtime code.
#![forbid(unsafe_code)]
pub use regex;
pub use serde_json as json;
pub mod archive;
pub mod bindings;
pub mod composition;
pub mod configuration;
pub mod native;
mod native_nuttx;
pub mod nuttx_resources;
mod overlay;
pub mod preparation;
pub mod prepared_resource;
pub mod profile;
pub mod resolved;
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

/// Emit the validated simulation seed and endpoint identities for a HAL build.
pub fn write_simulation_config(profile: &profile::Profile, out: &Path) -> Result<()> {
    let composition = profile
        .composition()?
        .ok_or("simulation configuration requires a peripheral composition")?;
    let spec = composition
        .simulation()
        .ok_or("profile does not select simulation")?;
    let sources = spec
        .endpoints
        .iter()
        .map(|source| match source {
            composition::SimulationEndpoint::Imu => "simulation::Source::Imu",
            composition::SimulationEndpoint::Gnss => "simulation::Source::Gnss",
        })
        .collect::<Vec<_>>()
        .join(", ");
    let generated = format!(
        "const SIM_SEED: u64 = {};\nconst SIM_SOURCES: &[simulation::Source] = &[{}];\n",
        spec.seed, sources
    );
    fs::write(out.join("simulation_config.rs"), generated).map_err(|error| error.to_string())
}

/// Resolve product profiles and pinned SDK inputs explicitly for external users.
/// In-tree builds may discover the checkout by its platform profiles.
pub fn sdk_root(manifest: &Path) -> Result<std::path::PathBuf> {
    fn valid(root: &Path) -> bool {
        root.join("Cargo.toml").is_file()
            && root.join("platform/profiles").is_dir()
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

/// Load and validate the same profile input for every package build adapter.
pub fn selected_profile(manifest: &Path) -> Result<Option<(std::path::PathBuf, profile::Profile)>> {
    use std::{env, path::PathBuf};
    println!("cargo:rerun-if-env-changed=NXRS_PLATFORM");
    println!("cargo:rerun-if-env-changed=NXRS_RESOLVED_PROFILE");
    let name = env::var_os("NXRS_PLATFORM");
    let receipt = env::var_os("NXRS_RESOLVED_PROFILE");
    let name = name
        .as_ref()
        .map(|name| name.to_str().ok_or("platform name must be UTF-8"))
        .transpose()?;
    if name.is_none() && receipt.is_none() {
        return Ok(None);
    }
    let root = sdk_root(manifest)?;
    let target = env::var("TARGET").map_err(|error| error.to_string())?;
    let os = env::var("CARGO_CFG_TARGET_OS").map_err(|error| error.to_string())?;
    let profile = if let Some(path) = receipt {
        let path = PathBuf::from(path);
        let label = path
            .to_str()
            .filter(|s| !s.contains(['\r', '\n']))
            .ok_or("NXRS_RESOLVED_PROFILE must be a UTF-8 path without line breaks")?;
        if !path.is_absolute() {
            return Err("NXRS_RESOLVED_PROFILE must be an absolute path".into());
        }
        println!("cargo:rerun-if-changed={label}");
        resolved::load(&path, &root, name, &target, &os)?
    } else {
        let profile = profile::Profile::resolve(&root, name.ok_or("missing platform name")?)?;
        if os == "nuttx" && profile.data.get("board").is_some() {
            return Err("NuttX profile builds require prepared inputs; use cargo nxrs build with --platform or --profile".into());
        }
        profile.validate_cargo_target(&target, &os)?;
        profile
    };
    Ok(Some((root, profile)))
}

/// Read the optional detector budget from the selected product profile.
pub fn detector_config(manifest: &Path) -> Result<Option<profile::Detector>> {
    let Some((_, profile)) = selected_profile(manifest)? else {
        return Ok(None);
    };
    for path in profile.source_paths() {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    profile.detector()
}

/// Link resource ABI glue prepared by the firmware frontend.
pub fn configure_resource() -> Result<()> {
    use std::{env, path::PathBuf};
    let directory = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").ok_or("missing manifest")?);
    let out = PathBuf::from(env::var_os("OUT_DIR").ok_or("missing OUT_DIR")?);
    let os = env::var("CARGO_CFG_TARGET_OS").map_err(|e| e.to_string())?;
    let target = env::var("TARGET").map_err(|e| e.to_string())?;
    for line in native::build(&directory, &os, &target, &out)? {
        println!("{line}");
    }
    Ok(())
}

fn capability_name(package: &str) -> Result<&str> {
    let capability = package
        .strip_prefix("nxrs-")
        .ok_or("capability package name")?;
    Ok(capability.strip_suffix("-devices").unwrap_or(capability))
}

/// Cargo adapter for shared profile, declarative configuration and native preparation.
/// Package-local adapters remain until the pre-Cargo compiler handoff is qualified.
pub fn configure_hal() -> Result<String> {
    use std::{env, path::PathBuf};
    println!(
        "cargo:rustc-check-cfg=cfg(platform, values(\"nuttx\", \"linux\", \"macos\", \"windows\", \"mock\", \"simulation\"))"
    );
    for name in [
        "CARGO_CFG_HAL_ENABLED",
        "CARGO_CFG_HAL_MOCK",
        "CARGO_CFG_NXRS_HAL_PLATFORM",
        "CARGO_CFG_NXRS_PROVIDER",
        "CARGO_CFG_HAL_PROVIDER",
        "CARGO_CFG_PLATFORM",
    ] {
        if env::var_os(name).is_some() {
            return Err("Select one NXRS_PLATFORM, not hand-written platform cfg flags".into());
        }
    }
    let directory =
        PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").ok_or("missing Cargo manifest directory")?);
    let Some((root, profile)) = selected_profile(&directory)? else {
        return Ok("none".into());
    };
    let package = env::var("CARGO_PKG_NAME").map_err(|e| e.to_string())?;
    let capability = capability_name(&package)?;
    let os = env::var("CARGO_CFG_TARGET_OS").map_err(|e| e.to_string())?;
    for line in profile.cargo_output(&root, capability, &os)? {
        println!("{line}")
    }
    let binding = profile.binding(capability);
    let package_bindings = bindings::Package::load(&directory)?;
    if binding == os && !package_bindings.driver_features.is_empty() {
        let graph = profile
            .composition()?
            .ok_or("driver features require composition")?;
        for device in graph.devices.values() {
            if let Some(feature) = package_bindings.driver_features.get(&device.compatible) {
                let variable =
                    format!("CARGO_FEATURE_{}", feature.replace('-', "_").to_uppercase());
                if env::var_os(&variable).is_none() {
                    return Err(format!("{package} is missing the profile-derived {feature} dependency; rerun cargo nxrs build"));
                }
            }
        }
    }
    let out = PathBuf::from(env::var_os("OUT_DIR").ok_or("missing OUT_DIR")?);
    for path in preparation::prepare_package(&directory, &profile, &os, &out)? {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    if binding == "simulation" {
        write_simulation_config(&profile, &out)?;
    }
    if ["macos", "linux", "nuttx"].contains(&binding) {
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
