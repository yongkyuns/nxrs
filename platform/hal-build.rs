//! Shared build-only selection. No platform dependency enters the runtime graph.
use std::{env, path::Path, process::Command};

fn main() {
    println!("cargo:rustc-check-cfg=cfg(nxrs_native_witness)");
    println!("cargo:rustc-check-cfg=cfg(nxrs_provider, values(\"none\", \"mock\", \"native\", \"nuttx\"))");
    println!("cargo:rerun-if-env-changed=NXRS_PLATFORM");
    println!("cargo:rerun-if-env-changed=NXRS_PYTHON");
    assert!(
        env::var_os("CARGO_CFG_NXRS_PROVIDER").is_none(),
        "Select one NXRS_PLATFORM, not hand-written provider cfg flags"
    );
    let Some(platform) = env::var_os("NXRS_PLATFORM") else {
        // Ordinary portable-library builds select no hardware and no mock.
        println!("cargo:rustc-cfg=nxrs_provider=\"none\"");
        return;
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
    print!(
        "{}",
        String::from_utf8(output.stdout).expect("platform resolver UTF-8")
    );
}
