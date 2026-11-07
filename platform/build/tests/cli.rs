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

fn write_imu_fixture(root: &Path) {
    fs::create_dir_all(root.join("hal/imu")).unwrap();
    fs::write(
        root.join("hal/imu/Cargo.toml"),
        "[package]\nname = \"nxrs-imu\"\nversion = \"0.1.0\"\n[package.metadata.nxrs]\nplatforms = [\"nuttx\"]\n",
    )
    .unwrap();
}

fn write_imu_profile(root: &Path, name: &str, body: &str) {
    fs::write(
        root.join(format!("platform/profiles/{name}.toml")),
        format!(
            "target-os = [\"nuttx\"]\nexecution = \"nuttx\"\nboard = \"fixture:board\"\ntarget = \"thumbv8m.main-nuttx-eabi\"\ncrossdev = \"arm-none-eabi-\"\n[hal]\nimu = \"nuttx\"\n{body}"
        ),
    )
    .unwrap();
}

fn legacy_imu_profile() -> &'static str {
    "[imu]\ndriver = \"bmi270\"\ndevice = \"/dev/i2c0\"\naddress = 104\nfrequency = 400000\nstack-bytes = 16384\n"
}

fn typed_imu_profile(mode: &str) -> String {
    format!(
        "[composition]\nversion = 1\nmode = \"{mode}\"\n\
         [resources.sensor_bus]\nkind = \"i2c\"\ndevice = \"/dev/i2c0\"\nfrequency = 400000\n\
         [devices.motion]\ncompatible = \"bosch,bmi270\"\nbus = \"sensor_bus\"\naddress = 104\n\
         [devices.motion.wake]\nkind = \"periodic\"\n\
         [roles]\nimu = \"motion\"\n\
         [imu]\nstack-bytes = 16384\n"
    )
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
fn profile_cli_unifies_legacy_and_v1_imu_and_reports_composition_provenance() {
    let root = fixture();
    write_imu_fixture(&root.0);
    write_imu_profile(&root.0, "legacy-imu", legacy_imu_profile());
    write_imu_profile(&root.0, "typed-imu", &typed_imu_profile("hardware"));

    let cargo_lines = |profile: &str| {
        success(frontend(
            &root.0,
            &root.0,
            &[
                "profile",
                "--platform",
                profile,
                "--target-os",
                "nuttx",
                "--capability",
                "imu",
            ],
        ))
        .lines()
        .filter(|line| line.starts_with("cargo:rustc-env=IMU_"))
        .map(str::to_owned)
        .collect::<Vec<_>>()
    };
    let legacy = cargo_lines("legacy-imu");
    let typed = cargo_lines("typed-imu");
    assert_eq!(legacy, typed);
    assert_eq!(
        legacy,
        [
            "cargo:rustc-env=IMU_DEVICE=/dev/i2c0",
            "cargo:rustc-env=IMU_ADDRESS=104",
            "cargo:rustc-env=IMU_FREQUENCY=400000",
            "cargo:rustc-env=IMU_STACK_BYTES=16384",
            "cargo:rustc-env=IMU_IRQ_DEVICE=",
        ]
    );

    let profile: Value = serde_json::from_str(&success(frontend(
        &root.0,
        &root.0,
        &["profile", "--platform", "typed-imu"],
    )))
    .unwrap();
    let composition = &profile["resolved-composition"];
    assert!(composition.is_object(), "{profile}");
    assert!(composition["graph"].is_object(), "{composition}");
    assert!(composition["sources"].is_array(), "{composition}");
    assert!(!composition["sources"].as_array().unwrap().is_empty());
    assert_eq!(
        composition["sources"][0],
        root.0
            .join("platform/profiles/typed-imu.toml")
            .canonicalize()
            .unwrap()
            .to_str()
            .unwrap()
    );
    assert_eq!(
        composition["binding-sources"][0],
        "hal/imu/bindings/bmi270-i2c.toml"
    );
}

#[test]
fn build_dry_run_admits_v1_required_imu_without_sdk_and_rejects_bad_default_roles() {
    let root = fixture();
    write_imu_fixture(&root.0);
    let manifest = root.0.join("app/probe/Cargo.toml");
    let original = fs::read_to_string(&manifest).unwrap();
    fs::write(
        &manifest,
        format!("{original}\n[package.metadata.nxrs]\nrequired-capabilities = [\"imu\"]\n"),
    )
    .unwrap();

    let mut no_role = typed_imu_profile("hardware");
    no_role = no_role.replace("imu = \"motion\"\n", "");
    write_imu_profile(&root.0, "missing-role", &no_role);
    let mut dangling_role = typed_imu_profile("hardware");
    dangling_role = dangling_role.replace("imu = \"motion\"", "imu = \"missing\"");
    write_imu_profile(&root.0, "dangling-role", &dangling_role);
    write_imu_profile(&root.0, "typed-imu", &typed_imu_profile("hardware"));

    for profile in ["missing-role", "dangling-role"] {
        let output = frontend(
            &root.0,
            &root.0,
            &[
                "build",
                "--manifest-path",
                "app/probe/Cargo.toml",
                "--profile",
                profile,
                "--dry-run",
            ],
        );
        assert!(output.stdout.is_empty(), "do not publish an invalid plan");
        let error = failure(output);
        assert!(error.contains("imu"), "{profile}: {error}");
    }

    let plan: Value = serde_json::from_str(&success(frontend(
        &root.0,
        &root.0,
        &[
            "build",
            "--manifest-path",
            "app/probe/Cargo.toml",
            "--profile",
            "typed-imu",
            "--dry-run",
        ],
    )))
    .unwrap();
    assert_eq!(plan["backend"], "nuttx");
    assert_eq!(plan["deployment"]["providers"][0]["capability"], "imu");
    assert!(!root.0.join("target").exists());
}

#[test]
fn qualification_composition_requires_an_explicit_qualification_application() {
    let root = fixture();
    write_imu_fixture(&root.0);
    write_imu_profile(
        &root.0,
        "imu-qualification",
        &typed_imu_profile("qualification"),
    );
    let manifest = root.0.join("app/probe/Cargo.toml");
    let original = fs::read_to_string(&manifest).unwrap();
    let args = [
        "build",
        "--manifest-path",
        "app/probe/Cargo.toml",
        "--profile",
        "imu-qualification",
        "--dry-run",
    ];

    let error = failure(frontend(&root.0, &root.0, &args));
    assert!(error.contains("qualification-only"), "{error}");

    fs::write(
        &manifest,
        format!(
            "{original}\n[package.metadata.nxrs]\nqualification-only = true\nrequired-capabilities = [\"imu\"]\n"
        ),
    )
    .unwrap();
    let plan: Value = serde_json::from_str(&success(frontend(&root.0, &root.0, &args))).unwrap();
    assert_eq!(plan["backend"], "nuttx");
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
fn malformed_application_metadata_fails_before_build_planning() {
    let root = fixture();
    let manifest = root.0.join("app/probe/Cargo.toml");
    let original = fs::read_to_string(&manifest).unwrap();
    for (metadata, expected) in [
        (
            "[package.metadata]\nnxrs = \"bad\"\n",
            "package.metadata.nxrs must be a table",
        ),
        (
            "[package.metadata]\nnxrs = [\"bad\"]\n",
            "package.metadata.nxrs must be a table",
        ),
        (
            "[package.metadata.nxrs.firmare]\nstack-size = 16384\n",
            "package.metadata.nxrs.firmare",
        ),
        (
            "[package.metadata.nxrs]\npriority = 80\n",
            "package.metadata.nxrs.priority",
        ),
        (
            "[package.metadata.nxrs]\nrequired-capabilites = [\"camera\"]\n",
            "package.metadata.nxrs.required-capabilites",
        ),
        (
            "[package.metadata.nxrs]\nqualification-only = \"true\"\n",
            "package.metadata.nxrs.qualification-only must be a boolean",
        ),
    ] {
        fs::write(&manifest, format!("{original}\n{metadata}")).unwrap();
        let output = frontend(
            &root.0,
            &root.0,
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
        );
        assert!(output.stdout.is_empty(), "do not publish a defaulted plan");
        let error = failure(output);
        assert!(error.contains(expected), "{metadata}: {error}");
        assert!(!root.0.join("artifacts").exists());
    }
}

#[test]
fn deployment_preflight_rejects_misspelled_requirements_without_evidence() {
    let root = fixture();
    let manifest = root.0.join("app/probe/Cargo.toml");
    let original = fs::read_to_string(&manifest).unwrap();
    fs::write(
        &manifest,
        format!("{original}\n[package.metadata.nxrs]\nrequired-capabilites = [\"camera\"]\n"),
    )
    .unwrap();
    let output = frontend(
        &root.0,
        &root.0,
        &[
            "check",
            "--manifest-path",
            "app/probe/Cargo.toml",
            "--profile",
            "host",
            "--target-os",
            env::consts::OS,
            "--out",
            "preflight.json",
        ],
    );
    assert!(
        output.stdout.is_empty(),
        "do not publish admission evidence"
    );
    let error = failure(output);
    assert!(
        error.contains("package.metadata.nxrs.required-capabilites"),
        "{error}"
    );
    assert!(!root.0.join("preflight.json").exists());
}

#[test]
fn valid_application_metadata_and_qualification_fixture_keep_working() {
    let root = fixture();
    let manifest = root.0.join("app/probe/Cargo.toml");
    let original = fs::read_to_string(&manifest).unwrap();
    for (metadata, command) in [
        ("", "cli_probe"),
        (
            "[package.metadata.other_tool]\nfirmare = \"independent\"\n",
            "cli_probe",
        ),
        ("[package.metadata.nxrs]\n", "cli_probe"),
        (
            "[package.metadata.nxrs]\nqualification-only = false\n",
            "cli_probe",
        ),
        (
            "[package.metadata.nxrs]\nrequired-capabilities = []\nqualification-only = true\n\
             [package.metadata.nxrs.firmware]\ncommand = \"start_probe\"\npriority = 80\nstack-size = 16384\n",
            "start_probe",
        ),
    ] {
        fs::write(&manifest, format!("{original}\n{metadata}")).unwrap();
        let output = success(frontend(
            &root.0,
            &root.0,
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
        assert_eq!(plan["bin"], "cli-probe");
        assert_eq!(plan["command"], command);
        assert!(!root.0.join("artifacts").exists());
    }

    // Preserve the real qualification entry, not only a synthetic metadata table.
    let repository = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let output = success(frontend(
        &repository,
        &root.0,
        &[
            "build",
            "--manifest-path",
            "tests/service-footprint/Cargo.toml",
            "--profile",
            env::consts::OS,
            "--dry-run",
        ],
    ));
    let plan: Value = serde_json::from_str(&output).unwrap();
    assert_eq!(plan["bin"], "cq-scale");
    assert_eq!(plan["command"], "cq_scale");
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
