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
fn profile_cli_reports_the_resolved_imu_and_its_provenance() {
    let root = fixture();
    write_imu_fixture(&root.0);
    write_imu_profile(&root.0, "typed-imu", &typed_imu_profile("hardware"));

    let cargo_output = success(frontend(
        &root.0,
        &root.0,
        &[
            "profile",
            "--platform",
            "typed-imu",
            "--target-os",
            "nuttx",
            "--capability",
            "imu",
        ],
    ));
    assert!(cargo_output
        .lines()
        .any(|line| line == "cargo:rustc-cfg=hal_provider=\"hardware\""));
    assert!(!cargo_output
        .lines()
        .any(|line| line.starts_with("cargo:rustc-env=IMU_")));

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
        "hal/imu/bindings/i2c.toml"
    );
}

#[test]
fn profile_applies_declared_and_cli_overlays_in_order_with_raw_and_semantic_identity() {
    let root = fixture();
    write_imu_fixture(&root.0);
    fs::create_dir_all(root.0.join("platform/profiles/patches")).unwrap();
    fs::create_dir_all(root.0.join("overlays")).unwrap();

    // Named overlay paths are relative to the profile that declares them.
    fs::write(
        root.0.join("platform/profiles/base.toml"),
        format!(
            "target-os = [\"nuttx\"]\nexecution = \"nuttx\"\nboard = \"fixture:board\"\n\
             target = \"thumbv8m.main-nuttx-eabi\"\ncrossdev = \"arm-none-eabi-\"\n\
             overlays = [\"patches/ancestor.toml\"]\n[hal]\nimu = \"nuttx\"\n{}",
            typed_imu_profile("hardware")
        ),
    )
    .unwrap();
    fs::write(
        root.0.join("platform/profiles/derived.toml"),
        "extends = \"base\"\noverlays = [\"patches/child.toml\"]\n",
    )
    .unwrap();

    let address_overlay = |address| format!("[devices.motion]\naddress = {address}\n");
    fs::write(
        root.0.join("platform/profiles/patches/ancestor.toml"),
        address_overlay(105),
    )
    .unwrap();
    fs::write(
        root.0.join("platform/profiles/patches/child.toml"),
        address_overlay(104),
    )
    .unwrap();
    fs::write(root.0.join("overlays/first.toml"), address_overlay(104)).unwrap();
    fs::write(root.0.join("overlays/last.toml"), address_overlay(105)).unwrap();

    let outside = TempDir::new(&env::temp_dir()).unwrap();
    let inspect = || {
        let output = success(frontend(
            &root.0,
            &outside.0,
            &[
                "profile",
                "--platform",
                "derived",
                "--overlay",
                "overlays/first.toml",
                "--overlay",
                "overlays/last.toml",
            ],
        ));
        serde_json::from_str::<Value>(&output).unwrap()
    };
    let profile = inspect();
    let composition = &profile["resolved-composition"];
    assert_eq!(composition["graph"]["devices"]["motion"]["address"], 105);
    assert_eq!(
        composition["provenance"]["devices.motion.address"],
        root.0
            .join("overlays/last.toml")
            .canonicalize()
            .unwrap()
            .to_str()
            .unwrap()
    );
    let hashes = composition["source-hashes"].as_array().unwrap();
    let expected = [
        ("platform/profiles/base.toml", "profile"),
        ("platform/profiles/derived.toml", "profile"),
        ("patches/ancestor.toml", "declared-overlay"),
        ("patches/child.toml", "declared-overlay"),
        ("overlays/first.toml", "command-line-overlay"),
        ("overlays/last.toml", "command-line-overlay"),
    ];
    assert_eq!(hashes.len(), expected.len());
    for (source, (suffix, kind)) in hashes.iter().zip(expected) {
        assert!(Path::new(source["path"].as_str().unwrap()).ends_with(suffix));
        assert_eq!(source["kind"], kind);
        assert!(source["sha256"]
            .as_str()
            .is_some_and(|hash| hash.len() == 64));
    }

    let semantic = composition["semantic-digest"].as_str().unwrap().to_owned();
    let fingerprint = composition["input-fingerprint"]
        .as_str()
        .unwrap()
        .to_owned();
    fs::write(
        root.0.join("overlays/last.toml"),
        format!(
            "# formatting and comments do not change the graph\n{}\n",
            address_overlay(105)
        ),
    )
    .unwrap();
    let edited = inspect();
    assert_eq!(edited["resolved-composition"]["semantic-digest"], semantic);
    assert_ne!(
        edited["resolved-composition"]["input-fingerprint"],
        fingerprint
    );

    let reversed = success(frontend(
        &root.0,
        &outside.0,
        &[
            "profile",
            "--platform",
            "derived",
            "--overlay",
            "overlays/last.toml",
            "--overlay",
            "overlays/first.toml",
        ],
    ));
    let reversed: Value = serde_json::from_str(&reversed).unwrap();
    assert_eq!(
        reversed["resolved-composition"]["graph"]["devices"]["motion"]["address"],
        104
    );
    assert_ne!(
        reversed["resolved-composition"]["semantic-digest"],
        semantic
    );
}

#[test]
fn native_endpoint_overlays_agree_in_profile_and_cargo_output() {
    let root = fixture();
    for capability in ["camera", "ble"] {
        let hal = root.0.join("hal").join(capability);
        fs::create_dir_all(&hal).unwrap();
        fs::write(
            hal.join("Cargo.toml"),
            "[package.metadata.nxrs]\nplatforms = [\"nuttx\"]\n",
        )
        .unwrap();
    }
    fs::write(
        root.0.join("platform/profiles/native-endpoints.toml"),
        "target-os = [\"nuttx\"]\n[hal]\ncamera = \"nuttx\"\nble = \"nuttx\"\n",
    )
    .unwrap();
    fs::write(
        root.0.join("endpoints.toml"),
        "[camera]\ndevice = \"/dev/video2\"\n[ble]\nscan-interface = \"bnep7\"\n",
    )
    .unwrap();
    let args = ["profile", "--platform", "native-endpoints"];
    let inspect = |extra: &[&str]| -> Value {
        let all = [&args[..], extra].concat();
        serde_json::from_str(&success(frontend(&root.0, &root.0, &all))).unwrap()
    };
    let defaults = inspect(&[]);
    assert_eq!(defaults["camera"]["device"], "/dev/video0");
    assert_eq!(defaults["ble"]["scan-interface"], "bnep0");

    let overlay = ["--overlay", "endpoints.toml"];
    let effective = inspect(&overlay);
    for (capability, key, value, variable) in [
        ("camera", "device", "/dev/video2", "CAMERA_DEVICE"),
        ("ble", "scan-interface", "bnep7", "BLE_SCAN_INTERFACE"),
    ] {
        assert_eq!(effective[capability][key], value);
        let all = [
            &args[..],
            &overlay,
            &["--capability", capability, "--target-os", "nuttx"],
        ]
        .concat();
        let output = success(frontend(&root.0, &root.0, &all));
        assert!(output
            .lines()
            .any(|line| line == format!("cargo:rustc-env={variable}={value}")));
    }

    fs::write(root.0.join("endpoints.toml"), "[hal]\ncamera = \"mock\"\n").unwrap();
    let error = failure(frontend(&root.0, &root.0, &[&args[..], &overlay].concat()));
    assert!(error.contains("overlays may only set camera or BLE endpoints"));
}

#[test]
fn cli_overlay_repairs_a_removed_role_and_dry_run_matches_inspection() {
    let root = fixture();
    write_imu_fixture(&root.0);
    fs::create_dir_all(root.0.join("overlays")).unwrap();
    write_imu_profile(&root.0, "typed-imu", &typed_imu_profile("hardware"));
    fs::write(
        root.0.join("overlays/remove-motion.toml"),
        "remove-devices = [\"motion\"]\n",
    )
    .unwrap();
    fs::write(
        root.0.join("overlays/add-replacement.toml"),
        "[devices.replacement]\ncompatible = \"bosch,bmi270\"\nbus = \"sensor_bus\"\naddress = 105\n\
         [devices.replacement.wake]\nkind = \"periodic\"\n\
         [roles]\nimu = \"replacement\"\n",
    )
    .unwrap();

    let outside = TempDir::new(&env::temp_dir()).unwrap();
    let dangling = failure(frontend(
        &root.0,
        &outside.0,
        &[
            "profile",
            "--platform",
            "typed-imu",
            "--overlay",
            "overlays/remove-motion.toml",
        ],
    ));
    assert!(
        dangling.contains("roles.imu") && dangling.contains("remove-motion.toml"),
        "{dangling}"
    );
    let profile_output = success(frontend(
        &root.0,
        &outside.0,
        &[
            "profile",
            "--platform",
            "typed-imu",
            "--overlay",
            "overlays/remove-motion.toml",
            "--overlay",
            "overlays/add-replacement.toml",
        ],
    ));
    let inspected: Value = serde_json::from_str(&profile_output).unwrap();
    assert_eq!(
        inspected["resolved-composition"]["graph"]["roles"]["imu"],
        "replacement"
    );
    assert!(inspected["resolved-composition"]["graph"]["devices"]["motion"].is_null());
    let plan_output = success(frontend(
        &root.0,
        &root.0,
        &[
            "build",
            "--manifest-path",
            "app/probe/Cargo.toml",
            "--profile",
            "typed-imu",
            "--overlay",
            "overlays/remove-motion.toml",
            "--overlay",
            "overlays/add-replacement.toml",
            "--dry-run",
        ],
    ));
    let plan: Value = serde_json::from_str(&plan_output).unwrap();
    assert_eq!(
        plan["deployment"]["resolved-composition"]["semantic-digest"],
        inspected["resolved-composition"]["semantic-digest"]
    );

    // A named overlay is resolved before a normal build reaches Cargo too.
    fs::create_dir_all(root.0.join("platform/profiles/overlays")).unwrap();
    fs::write(
        root.0.join("platform/profiles/overlays/invalid.toml"),
        "remove-devices = [\"missing\"]\n",
    )
    .unwrap();
    fs::write(
        root.0.join("platform/profiles/typed-imu.toml"),
        format!(
            "target-os = [\"nuttx\"]\nexecution = \"nuttx\"\nboard = \"fixture:board\"\n\
             target = \"thumbv8m.main-nuttx-eabi\"\ncrossdev = \"arm-none-eabi-\"\n\
             overlays = [\"overlays/invalid.toml\"]\n[hal]\nimu = \"nuttx\"\n{}",
            typed_imu_profile("hardware")
        ),
    )
    .unwrap();
    let output = frontend(
        &root.0,
        &outside.0,
        &[
            "build",
            "--manifest-path",
            "app/probe/Cargo.toml",
            "--profile",
            "typed-imu",
        ],
    );
    assert!(
        output.stdout.is_empty(),
        "do not plan an invalid named overlay"
    );
    let error = failure(output);
    assert!(error.contains("unknown devices ID 'missing'"), "{error}");

    fs::remove_file(root.0.join("platform/profiles/overlays/invalid.toml")).unwrap();
    let error = failure(frontend(
        &root.0,
        &outside.0,
        &["profile", "--platform", "typed-imu"],
    ));
    assert!(error.contains("invalid.toml"), "{error}");

    for (name, parent) in [("cycle-a", "cycle-b"), ("cycle-b", "cycle-a")] {
        fs::write(
            root.0.join(format!("platform/profiles/{name}.toml")),
            format!("extends = \"{parent}\"\n"),
        )
        .unwrap();
    }
    let error = failure(frontend(
        &root.0,
        &outside.0,
        &["profile", "--platform", "cycle-a"],
    ));
    assert!(error.contains("inheritance cycle"), "{error}");
}

#[test]
fn legacy_sensor_profile_fails_before_build_or_overlay() {
    let root = fixture();
    write_imu_fixture(&root.0);
    write_imu_profile(&root.0, "legacy-imu", legacy_imu_profile());
    let base = [
        "build",
        "--manifest-path",
        "app/probe/Cargo.toml",
        "--profile",
        "legacy-imu",
        "--dry-run",
    ];
    for extra in [&[][..], &["--overlay", "unused.toml"][..]] {
        let args: Vec<_> = base.into_iter().chain(extra.iter().copied()).collect();
        let error = failure(frontend(&root.0, &root.0, &args));
        assert!(error.contains("composition"), "{error}");
    }
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
    let receipt: Value =
        serde_json::from_slice(&fs::read(root.0.join("artifacts/resolved-profile.json")).unwrap())
            .unwrap();
    assert_eq!(receipt["profile"], "host");
    assert_eq!(receipt["os"], env::consts::OS);
    assert!(!outside.0.join("artifacts").exists());
}
