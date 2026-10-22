mod compile_model;
#[path = "../../platform/hal-build.rs"]
mod selection;
use std::{env, path::PathBuf, process::Command};
fn main() {
    let binding = selection::configure();
    println!("cargo:rustc-check-cfg=cfg(nxrs_detector_pico)");
    compile_model::main();
    if env::var_os("CARGO_FEATURE_PICO_MODEL").is_none()
        || !matches!(binding.as_str(), "std" | "nuttx")
    {
        return;
    }
    println!("cargo:rustc-cfg=nxrs_detector_pico");
    let directory = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let root = directory.parent().unwrap().parent().unwrap();
    for name in [
        "NXRS_PICO_SDK",
        "NXRS_PYTHON",
        "CXX",
        "AR",
        "NXRS_NUTTX_INCLUDE",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    for path in [
        directory.join("src/platform/std/ffi"),
        root.join("upstream/esp-dl"),
    ] {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    let sdk = env::var_os("NXRS_PICO_SDK")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("target/pico-sdk"));
    println!("cargo:rerun-if-changed={}", sdk.display());
    let status = Command::new(env::var_os("NXRS_PYTHON").unwrap_or_else(|| "python3".into()))
        .arg(directory.join("src/platform/std/ffi/build.py"))
        .arg("--sdk")
        .arg(sdk)
        .arg("--out")
        .arg(env::var_os("OUT_DIR").unwrap())
        .arg("--target")
        .arg(env::var_os("TARGET").unwrap())
        .status()
        .unwrap();
    assert!(
        status.success(),
        "PICO SDK build failed; run python3 upstream/esp-dl/prepare.py first"
    );
}
