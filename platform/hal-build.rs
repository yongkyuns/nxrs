//! Shared build-only selection. No platform dependency enters the runtime graph.
use std::{env, path::Path, process::Command};

// Also included as a module by compound build scripts, which call configure.
#[allow(dead_code)]
pub fn main() {
    configure();
}

/// Configure one HAL and return its resolved binding to compound build scripts.
pub fn configure() -> String {
    println!("cargo:rustc-check-cfg=cfg(native_witness)");
    println!("cargo:rustc-check-cfg=cfg(hal_enabled)");
    println!("cargo:rustc-check-cfg=cfg(hal_mock)");
    println!("cargo:rerun-if-env-changed=NXRS_PLATFORM");
    println!("cargo:rerun-if-env-changed=NXRS_PYTHON");
    assert!(
        env::var_os("CARGO_CFG_HAL_ENABLED").is_none()
            && env::var_os("CARGO_CFG_HAL_MOCK").is_none()
            && env::var_os("CARGO_CFG_NXRS_HAL_PLATFORM").is_none()
            && env::var_os("CARGO_CFG_NXRS_PROVIDER").is_none(),
        "Select one NXRS_PLATFORM, not hand-written provider cfg flags"
    );
    let Some(platform) = env::var_os("NXRS_PLATFORM") else {
        // Ordinary portable-library builds select no hardware and no mock.
        return "none".into();
    };
    let manifest_dir = env::var("CARGO_MANIFEST_DIR").unwrap();
    let root = Path::new(&manifest_dir).parent().unwrap().parent().unwrap();
    let resolver = root.join("tools/platform_config.py");
    println!("cargo:rerun-if-changed={}", resolver.display());
    println!(
        "cargo:rerun-if-changed={}",
        root.join("tools/hal_native.py").display()
    );
    let package = env::var("CARGO_PKG_NAME").unwrap();
    let capability = package.strip_prefix("nxrs-").expect("HAL package name");
    let output = Command::new(env::var_os("NXRS_PYTHON").unwrap_or_else(|| "python3".into()))
        .arg(&resolver)
        .arg("--emit-cargo")
        .arg("--platform")
        .arg(platform)
        .arg("--capability")
        .arg(capability)
        .arg("--target-os")
        .arg(env::var("CARGO_CFG_TARGET_OS").unwrap())
        .arg("--native-out")
        .arg(env::var("OUT_DIR").unwrap())
        .arg("--target-triple")
        .arg(env::var("TARGET").unwrap())
        .output()
        .expect("Explicit platform builds require Python 3.11+ (tomllib)");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let output = String::from_utf8(output.stdout).expect("platform resolver UTF-8");
    let binding = if output
        .lines()
        .any(|line| line == "cargo:rustc-cfg=hal_mock")
    {
        "mock".to_owned()
    } else if output
        .lines()
        .any(|line| line == "cargo:rustc-cfg=hal_enabled")
    {
        env::var("CARGO_CFG_TARGET_OS").unwrap()
    } else {
        "none".to_owned()
    };
    print!("{output}");
    binding
}
