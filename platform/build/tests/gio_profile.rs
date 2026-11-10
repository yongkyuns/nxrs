//! Run the real GIO adapter with a validated receipt and no profile-name variable.
use nxrs_build_support::{profile::Profile, resolved, TempDir};
use std::{fs, path::Path, process::Command};

#[test]
fn gio_validates_receipt_only_selection_before_requesting_native_tools() {
    let sdk = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let temp = TempDir::new(&std::env::temp_dir()).unwrap();
    let build = Command::new(env!("CARGO"))
        .current_dir(&sdk)
        .args([
            "check",
            "--offline",
            "--locked",
            "-p",
            "nxrs-linux-gio",
            "--features",
            "wifi",
            "--target-dir",
        ])
        .arg(&temp.0)
        .env_remove("NXRS_PLATFORM")
        .env_remove("NXRS_RESOLVED_PROFILE")
        .output()
        .unwrap();
    assert!(
        build.status.success(),
        "{}",
        String::from_utf8_lossy(&build.stderr)
    );
    let executable = fs::read_dir(temp.0.join("debug/build"))
        .unwrap()
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("nxrs-linux-gio-")
        })
        .map(|p| {
            p.join(if cfg!(windows) {
                "build-script-build.exe"
            } else {
                "build-script-build"
            })
        })
        .find(|p| p.is_file())
        .unwrap();
    let receipt = temp.0.join("profile.json");
    let profile = Profile::resolve(&sdk, "linux").unwrap();
    resolved::write(
        &receipt,
        &sdk,
        &profile,
        "x86_64-unknown-linux-gnu",
        "linux",
    )
    .unwrap();
    let mut run = Command::new(executable);
    run.env_remove("NXRS_PLATFORM")
        .env("NXRS_RESOLVED_PROFILE", &receipt)
        .env("NXRS_SDK_ROOT", &sdk)
        .env("CARGO_MANIFEST_DIR", sdk.join("driver/linux/gio"))
        .env("CARGO_CFG_TARGET_OS", "linux")
        .env("TARGET", "x86_64-unknown-linux-gnu")
        .env("OUT_DIR", temp.0.join("native"))
        .env("PKG_CONFIG", temp.0.join("missing-pkg-config"));
    let selected = run.output().unwrap();
    // Reaching the requested tool proves receipt-only selection admitted GIO.
    // No Linux headers/compiler or host GLib installation are needed for this test.
    assert!(!selected.status.success());
    let native_boundary = if cfg!(target_os = "linux") {
        "missing-pkg-config"
    } else {
        "native linux sources require a linux host"
    };
    assert!(
        String::from_utf8_lossy(&selected.stderr).contains(native_boundary),
        "{}",
        String::from_utf8_lossy(&selected.stderr)
    );
    run.env("TARGET", "aarch64-unknown-linux-gnu");
    let mismatch = run.output().unwrap();
    assert!(!mismatch.status.success());
    assert!(!String::from_utf8_lossy(&mismatch.stderr).contains("missing-pkg-config"));
    run.env_remove("NXRS_RESOLVED_PROFILE");
    assert!(run.output().unwrap().status.success());
}
