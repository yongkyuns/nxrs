//! Run the real GIO adapter with a validated receipt and no profile-name variable.
use nxrs_build_support::{profile::Profile, resolved, shell_quote, TempDir};
use std::{path::Path, process::Command};

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
            "--message-format=json",
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
    let script = String::from_utf8_lossy(&build.stdout)
        .lines()
        .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
        .find(|record| {
            record["reason"] == "compiler-artifact"
                && record["target"]["kind"][0] == "custom-build"
                && record["package_id"].as_str().unwrap().contains("nxrs-linux-gio")
        })
        .unwrap();
    let executable = script["filenames"][0].as_str().unwrap();
    let receipt = temp.0.join("profile.json");
    let profile = Profile::resolve(&sdk, "linux").unwrap();
    let arch = std::env::consts::ARCH;
    let target = format!("{arch}-unknown-linux-gnu");
    resolved::write(&receipt, &sdk, &profile, &target, "linux").unwrap();
    let missing_tool = temp.0.join("missing-pkg-config");
    let mut run = Command::new(executable);
    run.env_remove("NXRS_PLATFORM")
        .env("NXRS_RESOLVED_PROFILE", &receipt)
        .env("NXRS_SDK_ROOT", &sdk)
        .env("CARGO_MANIFEST_DIR", sdk.join("driver/linux/gio"))
        .env("CARGO_CFG_TARGET_OS", "linux")
        .env("TARGET", &target)
        .env("OUT_DIR", temp.0.join("native"))
        .env("PKG_CONFIG", shell_quote(missing_tool.to_str().unwrap()));
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
    run.env("TARGET", format!("{arch}-unknown-linux-musl"));
    let mismatch = run.output().unwrap();
    assert!(!mismatch.status.success());
    assert!(String::from_utf8_lossy(&mismatch.stderr)
        .contains("resolved profile receipt target does not match this Cargo build"));
    run.env_remove("NXRS_RESOLVED_PROFILE");
    assert!(run.output().unwrap().status.success());
}
