#[path = "../../platform/hal-build.rs"]
mod selection;
use std::{env, path::PathBuf, process::Command};
fn main() {
    if selection::configure() == "none" {
        return;
    }
    let directory = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    for name in [
        "NXRS_TLS_ARCHIVE",
        "NXRS_NUTTX_INCLUDE",
        "CC",
        "AR",
        "CFLAGS",
        "CMAKE",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    for path in [
        directory.join("src/sdk/ffi"),
        directory.join("build-sdk.py"),
        directory.join("../../upstream/mbedtls/source.json"),
        directory.join("../../tests/tls/ffi"),
    ] {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    let output = Command::new(env::var_os("NXRS_PYTHON").unwrap_or_else(|| "python3".into()))
        .arg(directory.join("build-sdk.py"))
        .arg("--out")
        .arg(env::var_os("OUT_DIR").unwrap())
        .arg("--target")
        .arg(env::var("TARGET").unwrap())
        .output()
        .expect("TLS SDK build requires Python 3.12+ and CMake");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    print!("{}", String::from_utf8(output.stdout).unwrap());
}
