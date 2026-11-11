//! Exercise the public frontend, including selection before any target SDK is needed.
use nxrs_build::TempDir;
use serde_json::Value;
use std::{
    env, fs,
    path::Path,
    process::{Command, Output},
};

fn host_profile(root: &Path) -> std::path::PathBuf {
    root.join("platform").join(env::consts::OS).join("profile.toml")
}

fn fixture() -> TempDir {
    let root = TempDir::new(&env::temp_dir()).unwrap();
    fs::create_dir_all(root.0.join("tests/profiles")).unwrap();
    fs::create_dir_all(host_profile(&root.0).parent().unwrap()).unwrap();
    fs::create_dir_all(root.0.join("hal")).unwrap();
    fs::create_dir_all(root.0.join("app/probe/src")).unwrap();
    fs::write(root.0.join("Cargo.toml"), "[workspace]\nresolver = \"2\"\n").unwrap();
    fs::write(
        host_profile(&root.0),
        format!(
            "name = \"host\"\ntarget-os = [\"{os}\"]\nexecution = \"std\"\n[hal]\n",
            os = env::consts::OS
        ),
    )
    .unwrap();
    fs::write(
        root.0.join("tests/profiles/browser.toml"),
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

#[test]
fn editor_switch_preserves_project_settings_on_invalid_build() {
    let root = fixture();
    let editor = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_nxrs-build"))
            .current_dir(&root.0)
            .arg("editor")
            .args(args)
            .output()
            .unwrap()
    };
    assert_eq!(
        success(editor(&["status"])).trim(),
        "No Nxrs editor profile selected"
    );
    assert_eq!(success(editor(&["host"])).trim(), "Zed Rust profile: host");
    let path = root.0.join(".zed/settings.json");
    let before = fs::read(&path).unwrap();
    let settings: Value = serde_json::from_slice(&before).unwrap();
    assert!(settings["lsp"]["clangd"].is_object());
    assert_eq!(
        settings["lsp"]["rust-analyzer"]["initialization_options"]["cargo"]["extraEnv"]["NXRS_PLATFORM"],
        "host"
    );
    assert!(!editor(&["--build-dir", "missing"]).status.success());
    assert_eq!(success(editor(&["status"])).trim(), "Zed Rust profile: host");
    assert_eq!(fs::read(path).unwrap(), before);
}

fn write_imu_fixture(root: &Path) {
    use std::io::Write;
    writeln!(
        fs::OpenOptions::new()
            .append(true)
            .open(root.join("Cargo.toml"))
            .unwrap(),
        "\n[workspace.metadata.nxrs]\ndevice-catalogue = 'platform'"
    )
    .unwrap();
    let sdk = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    fs::create_dir_all(root.join("platform/nuttx/resources")).unwrap();
    fs::write(
        root.join("platform/nuttx/resources/fixture.toml"),
        "[resources.i2c0]\nkind = \"i2c\"\ndevice = \"/dev/i2c0\"\n",
    )
    .unwrap();
    for package in [
        "hal/imu",
        "platform",
        "driver/bmi270",
        "driver/lsm6dso",
    ] {
        let directory = root.join(package);
        fs::create_dir_all(&directory).unwrap();
        fs::copy(
            sdk.join(package).join("Cargo.toml"),
            directory.join("Cargo.toml"),
        )
        .unwrap();
        if package.starts_with("driver/") {
            fs::copy(
                sdk.join(package).join("bindings.toml"),
                directory.join("bindings.toml"),
            )
            .unwrap();
        }
    }
}

fn write_imu_profile(root: &Path, name: &str, body: &str) {
    fs::write(
        root.join(format!("tests/profiles/{name}.toml")),
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
         [resources.sensor_bus]\nbinding = \"i2c0\"\nfrequency = 400000\n\
         [devices.imu]\ncompatible = \"bosch,bmi270\"\nbus = \"sensor_bus\"\naddress = 104\n\
         [devices.imu.wake]\nkind = \"periodic\"\n\
         [aliases]\nimu = \"imu\"\n\
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
fn platform_defaults_are_declared_by_profiles() {
    let root = fixture();
    let platform = env::consts::OS;
    let run = |args: &[&str]| frontend(&root.0, &root.0, args);
    let fixtures = root.0.join("tests/profiles");
    fs::create_dir_all(&fixtures).unwrap();
    let variant = fixtures.join("variant.toml");
    fs::write(&variant, "extends = \"host\"\n").unwrap();
    assert_eq!(success(run(&["platforms"])).trim(), platform);
    let profile: Value =
        serde_json::from_str(&success(run(&["profile", "--platform", platform]))).unwrap();
    assert_eq!(profile["name"], "host");
    assert!(profile.get("default-for").is_none());
    for (explicit, expected) in [(None, "host"), (Some("variant"), "variant")] {
        let mut args = vec![
            "build",
            "--app",
            "probe",
            "--platform",
            platform,
            "--dry-run",
        ];
        if let Some(explicit) = explicit {
            args.extend(["--profile", explicit]);
        }
        let build: Value = serde_json::from_str(&success(run(&args))).unwrap();
        assert_eq!(build["deployment"]["platform"], expected);
    }
    assert!(failure(run(&[
        "build",
        "--app",
        "probe",
        "--platform",
        platform,
        "--profile",
        "browser",
        "--dry-run",
    ]))
    .contains("another platform family"));
    let duplicate = root.0.join("platform/other/profile.toml");
    fs::create_dir_all(duplicate.parent().unwrap()).unwrap();
    fs::write(&duplicate, "name = \"host\"\n").unwrap();
    assert!(failure(run(&["platforms"])).contains("ambiguous platform: host"));
    success(run(&["profile", "--platform", "browser"]));
    fs::write(duplicate, "invalid = [").unwrap();
    assert!(failure(run(&["platforms"])).contains("profile.toml"));
    assert!(failure(run(&["profile", "--platform", "other"])).contains("profile.toml"));
    success(run(&["profile", "--platform", "host"]));
    success(run(&["build", "--app", "probe", "--platform", platform, "--dry-run"]));
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
        host_profile(&root.0)
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
        .any(|line| line == "cargo:rustc-cfg=platform=\"nuttx\""));
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
            .join("tests/profiles/typed-imu.toml")
            .canonicalize()
            .unwrap()
            .to_str()
            .unwrap()
    );
    assert_eq!(
        composition["binding-sources"],
        serde_json::json!([
            "driver/bmi270/bindings.toml",
            "driver/lsm6dso/bindings.toml"
        ])
    );
}

#[test]
fn profile_applies_declared_and_cli_overlays_in_order_with_raw_and_semantic_identity() {
    let root = fixture();
    write_imu_fixture(&root.0);
    fs::create_dir_all(root.0.join("tests/profiles/patches")).unwrap();
    fs::create_dir_all(root.0.join("overlays")).unwrap();

    // Named overlay paths are relative to the profile that declares them.
    fs::write(
        root.0.join("tests/profiles/base.toml"),
        format!(
            "target-os = [\"nuttx\"]\nexecution = \"nuttx\"\nboard = \"fixture:board\"\n\
             target = \"thumbv8m.main-nuttx-eabi\"\ncrossdev = \"arm-none-eabi-\"\n\
             overlays = [\"patches/ancestor.toml\"]\n[hal]\nimu = \"nuttx\"\n{}",
            typed_imu_profile("hardware")
        ),
    )
    .unwrap();
    fs::write(
        root.0.join("tests/profiles/derived.toml"),
        "extends = \"base\"\noverlays = [\"patches/child.toml\"]\n",
    )
    .unwrap();

    let address_overlay = |address| format!("[devices.imu]\naddress = {address}\n");
    fs::write(
        root.0.join("tests/profiles/patches/ancestor.toml"),
        address_overlay(105),
    )
    .unwrap();
    fs::write(
        root.0.join("tests/profiles/patches/child.toml"),
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
    assert_eq!(composition["graph"]["devices"]["imu"]["address"], 105);
    assert_eq!(
        composition["provenance"]["devices.imu.address"],
        root.0
            .join("overlays/last.toml")
            .canonicalize()
            .unwrap()
            .to_str()
            .unwrap()
    );
    let hashes = composition["source-hashes"].as_array().unwrap();
    let expected = [
        ("tests/profiles/base.toml", "profile"),
        ("tests/profiles/derived.toml", "profile"),
        ("patches/ancestor.toml", "declared-overlay"),
        ("patches/child.toml", "declared-overlay"),
        ("overlays/first.toml", "command-line-overlay"),
        ("overlays/last.toml", "command-line-overlay"),
        ("platform/nuttx/resources/fixture.toml", "bsp-resources"),
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
        reversed["resolved-composition"]["graph"]["devices"]["imu"]["address"],
        104
    );
    assert_ne!(
        reversed["resolved-composition"]["semantic-digest"],
        semantic
    );
}

#[test]
fn cli_overlay_repairs_a_removed_alias_and_dry_run_matches_inspection() {
    let root = fixture();
    write_imu_fixture(&root.0);
    fs::create_dir_all(root.0.join("overlays")).unwrap();
    write_imu_profile(&root.0, "typed-imu", &typed_imu_profile("hardware"));
    fs::write(
        root.0.join("overlays/remove-imu.toml"),
        "remove-devices = [\"imu\"]\n",
    )
    .unwrap();
    fs::write(
        root.0.join("overlays/add-replacement.toml"),
        "[devices.replacement]\ncompatible = \"bosch,bmi270\"\nbus = \"sensor_bus\"\naddress = 105\n\
         [devices.replacement.wake]\nkind = \"periodic\"\n\
         [aliases]\nimu = \"replacement\"\n",
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
            "overlays/remove-imu.toml",
        ],
    ));
    assert!(
        dangling.contains("aliases.imu") && dangling.contains("remove-imu.toml"),
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
            "overlays/remove-imu.toml",
            "--overlay",
            "overlays/add-replacement.toml",
        ],
    ));
    let inspected: Value = serde_json::from_str(&profile_output).unwrap();
    assert_eq!(
        inspected["resolved-composition"]["graph"]["aliases"]["imu"],
        "replacement"
    );
    assert!(inspected["resolved-composition"]["graph"]["devices"]["imu"].is_null());
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
            "overlays/remove-imu.toml",
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
    fs::create_dir_all(root.0.join("tests/profiles/overlays")).unwrap();
    fs::write(
        root.0.join("tests/profiles/overlays/invalid.toml"),
        "remove-devices = [\"missing\"]\n",
    )
    .unwrap();
    fs::write(
        root.0.join("tests/profiles/typed-imu.toml"),
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

    fs::remove_file(root.0.join("tests/profiles/overlays/invalid.toml")).unwrap();
    let error = failure(frontend(
        &root.0,
        &outside.0,
        &["profile", "--platform", "typed-imu"],
    ));
    assert!(error.contains("invalid.toml"), "{error}");

    for (name, parent) in [("cycle-a", "cycle-b"), ("cycle-b", "cycle-a")] {
        fs::write(
            root.0.join(format!("tests/profiles/{name}.toml")),
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
fn build_dry_run_admits_v1_required_imu_without_sdk_and_rejects_bad_default_aliases() {
    let root = fixture();
    write_imu_fixture(&root.0);
    let manifest = root.0.join("app/probe/Cargo.toml");
    let original = fs::read_to_string(&manifest).unwrap();
    fs::write(
        &manifest,
        format!("{original}\n[package.metadata.nxrs]\nrequired-capabilities = [\"imu\"]\n"),
    )
    .unwrap();

    let mut no_alias = typed_imu_profile("hardware");
    no_alias = no_alias.replace("imu = \"imu\"\n", "");
    write_imu_profile(&root.0, "single-device", &no_alias);
    no_alias.push_str("\n[devices.backup]\ncompatible = \"bosch,bmi270\"\nbus = \"sensor_bus\"\naddress = 105\n[devices.backup.wake]\nkind = \"periodic\"\n");
    write_imu_profile(&root.0, "ambiguous-default", &no_alias);
    let mut dangling_alias = typed_imu_profile("hardware");
    dangling_alias = dangling_alias.replace("imu = \"imu\"", "imu = \"missing\"");
    write_imu_profile(&root.0, "dangling-alias", &dangling_alias);
    write_imu_profile(&root.0, "typed-imu", &typed_imu_profile("hardware"));

    for profile in ["ambiguous-default", "dangling-alias"] {
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
            "single-device",
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
fn host_build_preserves_optional_workspace_edges_in_the_consumer_lockfile() {
    let root = fixture();
    let app = root.0.join("app/probe");
    let manifest = app.join("Cargo.toml");
    fs::write(
        &manifest,
        format!(
            "{}\n[dependencies]\nsensor={{path=\"sensor\"}}\n",
            fs::read_to_string(&manifest).unwrap()
        ),
    )
    .unwrap();
    for (name, dependencies) in [
        (
            "sensor",
            "[dependencies]\nmarker={path=\"../marker\",optional=true}\n",
        ),
        ("marker", ""),
    ] {
        fs::create_dir_all(app.join(name).join("src")).unwrap();
        fs::write(
            app.join(name).join("Cargo.toml"),
            format!(
                "[package]\nname=\"{name}\"\nversion=\"0.0.0\"\nedition=\"2021\"\n{dependencies}"
            ),
        )
        .unwrap();
        fs::write(app.join(name).join("src/lib.rs"), "").unwrap();
    }
    success(
        Command::new(env!("CARGO"))
            .current_dir(&app)
            .args(["generate-lockfile", "--offline"])
            .output()
            .unwrap(),
    );
    let lock = fs::read(app.join("Cargo.lock")).unwrap();
    // App loading canonicalizes this path. On Windows, Cargo 1.90 can drop
    // optional workspace edges when given that verbatim spelling.
    success(frontend(
        &root.0,
        &root.0,
        &[
            "build",
            "--app",
            "probe",
            "--profile",
            "host",
            "--out",
            "artifacts",
        ],
    ));
    assert_eq!(fs::read(app.join("Cargo.lock")).unwrap(), lock);
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
