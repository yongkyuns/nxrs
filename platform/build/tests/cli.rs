//! Exercise the public frontend, including selection before any target SDK is needed.
use nxrs_build::TempDir;
use serde_json::Value;
use std::{
    env, fs,
    path::Path,
    process::{Command, Output},
};

fn fixture() -> TempDir {
    let root = TempDir::new(&env::temp_dir()).unwrap();
    fs::create_dir_all(root.0.join("platform/profiles")).unwrap();
    fs::create_dir_all(root.0.join("hal")).unwrap();
    fs::create_dir_all(root.0.join("app/probe/src")).unwrap();
    fs::write(root.0.join("Cargo.toml"), "[workspace]\nresolver = \"2\"\n").unwrap();
    fs::write(
        root.0.join("platform/profiles/host.toml"),
        format!(
            "target-os = [\"{}\"]\nexecution = \"std\"\n[hal]\n",
            env::consts::OS
        ),
    )
    .unwrap();
    fs::write(
        root.0.join("platform/profiles/browser.toml"),
        "target-os = [\"emscripten\"]\nexecution = \"web\"\ntarget = \"wasm32-unknown-emscripten\"\n[hal]\n",
    )
    .unwrap();
    fs::write(
        root.0.join("app/probe/Cargo.toml"),
        r#"[package]
name = "cli-probe"
version = "0.1.0"
edition = "2021"

[workspace]

[features]
default = ["default-marker"]
default-marker = []
first = []
second = []
third = []
fourth = []

[[bin]]
name = "cli-probe"
path = "src/main.rs"
"#,
    )
    .unwrap();
    fs::write(
        root.0.join("app/probe/Cargo.lock"),
        "version = 4\n\n[[package]]\nname = \"cli-probe\"\nversion = \"0.1.0\"\n",
    )
    .unwrap();
    fs::write(root.0.join("app/probe/src/main.rs"), "fn main() {}\n").unwrap();
    root
}

fn frontend(root: &Path, cwd: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_nxrs-build"))
        .current_dir(cwd)
        .arg("--workspace-root")
        .arg(root)
        .args(args)
        .output()
        .unwrap()
}

fn success(output: Output) -> String {
    assert!(
        output.status.success(),
        "frontend failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

fn failure(output: Output) -> String {
    assert!(
        !output.status.success(),
        "frontend unexpectedly succeeded: {}",
        String::from_utf8_lossy(&output.stdout)
    );
    String::from_utf8(output.stderr).unwrap()
}

#[test]
fn help_does_not_require_a_workspace() {
    let outside = TempDir::new(&env::temp_dir()).unwrap();
    for args in [
        vec![],
        vec!["--help"],
        vec!["-h"],
        vec!["build", "--help"],
        vec!["build", "-h"],
        vec!["profile", "--help"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_nxrs-build"))
            .current_dir(&outside.0)
            .args(&args)
            .output()
            .unwrap();
        let help = success(output);
        assert!(help.contains("usage:"), "{args:?}: {help}");
    }
    let help = success(frontend(
        &outside.0.join("missing-checkout"),
        &outside.0,
        &["build", "--help"],
    ));
    assert!(help.contains("--features <features>"));
    assert!(help.contains("--no-default-features"));
}

#[test]
fn profile_honors_the_explicit_root_from_another_directory() {
    let root = fixture();
    let outside = TempDir::new(&env::temp_dir()).unwrap();
    let output = success(frontend(
        &root.0,
        &outside.0,
        &["profile", "--platform", "host"],
    ));
    let profile: Value = serde_json::from_str(&output).unwrap();
    assert_eq!(profile["name"], "host");
    assert_eq!(profile["target-os"][0], env::consts::OS);
    assert_eq!(
        profile["sources"][0],
        root.0
            .join("platform/profiles/host.toml")
            .canonicalize()
            .unwrap()
            .to_str()
            .unwrap()
    );
}

#[test]
fn app_discovery_reports_a_malformed_manifest() {
    let root = fixture();
    fs::write(root.0.join("app/README.md"), "application examples\n").unwrap();
    fs::create_dir(root.0.join("app/assets")).unwrap();
    assert_eq!(success(frontend(&root.0, &root.0, &["apps"])), "probe\n");

    fs::create_dir(root.0.join("app/broken")).unwrap();
    let manifest = root.0.join("app/broken/Cargo.toml");
    fs::write(&manifest, "[package]\nname =\n").unwrap();
    // The frontend canonicalizes the workspace before discovering manifests.
    // Compare that same native spelling, including Windows verbatim paths.
    let manifest = manifest.canonicalize().unwrap();
    let output = frontend(&root.0, &root.0, &["apps"]);
    assert!(
        output.stdout.is_empty(),
        "do not publish a partial app list"
    );
    let error = failure(output);
    assert!(error.contains(manifest.to_str().unwrap()), "{error}");
    assert!(error.contains("invalid application"), "{error}");
}

#[test]
fn build_paths_are_relative_to_the_selected_root() {
    let root = fixture();
    let outside = TempDir::new(&env::temp_dir()).unwrap();
    let output = success(frontend(
        &root.0,
        &outside.0,
        &[
            "build",
            "--manifest-path",
            "app/probe/Cargo.toml",
            "--profile",
            "host",
            "--out",
            "artifacts",
            "--dry-run",
        ],
    ));
    let plan: Value = serde_json::from_str(&output).unwrap();
    let canonical = root.0.canonicalize().unwrap();
    assert_eq!(
        plan["deployment"]["app_manifest"],
        canonical.join("app/probe/Cargo.toml").to_str().unwrap()
    );
    assert_eq!(
        plan["output"],
        canonical.join("artifacts").to_str().unwrap()
    );
    assert!(!root.0.join("artifacts").exists());
    assert!(!outside.0.join("artifacts").exists());
}

#[test]
fn dry_run_rejects_options_for_another_backend() {
    let root = fixture();
    for profile in ["host", "browser"] {
        let error = failure(frontend(
            &root.0,
            &root.0,
            &[
                "build",
                "--app",
                "probe",
                "--profile",
                profile,
                "--kconfig-enable",
                "NSH_NETINIT",
                "--dry-run",
            ],
        ));
        assert!(error.contains("NuttX"), "{profile}: {error}");
    }
    for args in [
        vec!["--html", "template.html"],
        vec!["--unmodified-browser-std"],
    ] {
        let mut command = vec!["build", "--app", "probe", "--profile", "host", "--dry-run"];
        command.extend(args);
        let error = failure(frontend(&root.0, &root.0, &command));
        assert!(
            error.contains("browser switches require a web profile"),
            "{error}"
        );
    }
    assert!(!root.0.join("target").exists());
}

#[test]
fn feature_lists_preserve_the_profile_owned_model_policy() {
    let root = fixture();
    for flag in ["--feature", "--features"] {
        let error = failure(frontend(
            &root.0,
            &root.0,
            &["build", flag, "first, nxrs-detector/pico-model"],
        ));
        assert!(
            error.contains("model dependencies follow the profile"),
            "{error}"
        );
    }
    let error = failure(frontend(&root.0, &root.0, &["build", "--features", ",  "]));
    assert!(error.contains("missing Cargo feature list"), "{error}");
}

#[test]
fn cargo_receives_repeated_feature_lists_and_no_default_features() {
    let root = fixture();
    let outside = TempDir::new(&env::temp_dir()).unwrap();
    fs::write(
        root.0.join("app/probe/src/main.rs"),
        r#"#[cfg(feature = "default-marker")]
compile_error!("default features must be disabled");
#[cfg(not(all(feature = "first", feature = "second", feature = "third", feature = "fourth")))]
compile_error!("all requested features must reach Cargo");
fn main() {}
"#,
    )
    .unwrap();
    success(frontend(
        &root.0,
        &outside.0,
        &[
            "build",
            "--manifest-path",
            "app/probe/Cargo.toml",
            "--profile",
            "host",
            "--features",
            "first, second",
            "--features",
            "third",
            "--feature",
            "fourth",
            "--no-default-features",
            "--out",
            "artifacts",
        ],
    ));
    assert!(root
        .0
        .join("artifacts/cargo/release")
        .join(format!("cli-probe{}", env::consts::EXE_SUFFIX))
        .is_file());
    assert!(!outside.0.join("artifacts").exists());
}
