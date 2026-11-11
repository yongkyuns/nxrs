//! Cargo itself selects these fixture graphs; no SDK, registry, or native linker is needed.
use nxrs_build::{cargo_graph, profile::Profile};
use serde_json::json;
use std::{
    fs,
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture(PathBuf);
impl Fixture {
    fn new(dependencies: &str) -> Self {
        let directory = std::env::temp_dir().join(format!(
            "nxrs-cargo-graph-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let fixture = Self(directory);
        fixture.write(
            "sdk/Cargo.toml",
            "[workspace]\nmembers=[\"hal/detector\",\"service\"]\nresolver=\"2\"\n",
        );
        fixture.write("sdk/hal/detector/Cargo.toml", "[package]\nname=\"nxrs-detector\"\nversion=\"0.1.0\"\nedition=\"2024\"\n[features]\npico-model=[]\nperson-model=[]\n");
        fixture.write("sdk/hal/detector/src/lib.rs", "#[cfg(not(any(feature=\"pico-model\",feature=\"person-model\")))]\ncompile_error!(\"a selected detector must receive its profile model\");\npub fn detector() {}\n");
        fixture.write("sdk/service/Cargo.toml", "[package]\nname=\"detection-service\"\nversion=\"0.1.0\"\nedition=\"2024\"\n[dependencies]\ninternal_detector={package=\"nxrs-detector\",path=\"../hal/detector\"}\n");
        fixture.write("sdk/service/src/lib.rs", "pub fn detection() {}\n");
        fixture.write(
            "consumer/Cargo.toml",
            "[workspace]\nmembers=[\"app\"]\nresolver=\"2\"\n",
        );
        fixture.write(
            "consumer/app/Cargo.toml",
            &format!(
                "[package]\nname=\"consumer\"\nversion=\"0.1.0\"\nedition=\"2024\"\n{dependencies}"
            ),
        );
        fixture.write("consumer/app/src/main.rs", "fn main() {}\n");
        fixture
    }
    fn write(&self, name: &str, text: &str) {
        let path = self.0.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
    fn manifest(&self) -> PathBuf {
        self.0.join("consumer/app/Cargo.toml")
    }
    fn lock(&self) {
        succeeded(
            Command::new(env!("CARGO"))
                .current_dir(&self.0)
                .args(["generate-lockfile", "--offline", "--manifest-path"])
                .arg(self.manifest()),
        );
    }
    fn build(&self) -> Command {
        let mut command = Command::new(env!("CARGO"));
        command
            .current_dir(&self.0)
            .args(["build", "--offline", "--locked", "--manifest-path"])
            .arg(self.manifest())
            .args(["--bin", "consumer"])
            .env("CARGO_TARGET_DIR", self.0.join("target"));
        command
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn succeeded(command: &mut Command) {
    let result = command.output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
}
fn profile() -> Profile {
    Profile::from_data(
        json!({"name":"fixture","hal":{"detector":"mock"},"detector":{"model":"pico","stack-bytes":16384,"sample-ms":100}}),
        vec![],
    )
}
fn contains(selection: &cargo_graph::Selection, name: &str) -> bool {
    selection
        .packages
        .iter()
        .any(|package| package.starts_with(&format!("{name} v")))
}

#[test]
fn optional_detector_follows_default_and_explicit_cargo_features() {
    let fixture = Fixture::new(
        "[dependencies]\nvision={package=\"nxrs-detector\",path=\"../../sdk/hal/detector\",optional=true}\n[features]\ndefault=[\"detect\"]\ndetect=[\"dep:vision\"]\n",
    );
    fixture.lock();
    let mut disabled = fixture.build();
    disabled.arg("--no-default-features");
    let selection = cargo_graph::resolve(&mut disabled, &profile()).unwrap();
    assert!(!contains(&selection, "nxrs-detector"));
    assert!(selection.model_features.is_empty());
    succeeded(&mut disabled);

    let mut explicit = fixture.build();
    explicit.args(["--no-default-features", "--features", "detect"]);
    let selection = cargo_graph::resolve(&mut explicit, &profile()).unwrap();
    assert!(contains(&selection, "nxrs-detector"));
    assert_eq!(selection.model_features, ["vision?/pico-model"]);
    succeeded(&mut explicit);

    let mut defaults = fixture.build();
    let selection = cargo_graph::resolve(&mut defaults, &profile()).unwrap();
    assert_eq!(selection.model_features, ["vision?/pico-model"]);
    succeeded(&mut defaults);
}

#[test]
fn target_and_cargo_configuration_select_the_real_dependency_branch() {
    let fixture = Fixture::new(
        "[target.'cfg(target_os = \"windows\")'.dependencies]\nvision={package=\"nxrs-detector\",path=\"../../sdk/hal/detector\"}\n",
    );
    fixture.lock();
    let mut inactive = fixture.build();
    inactive.args(["--target", "x86_64-unknown-linux-gnu"]);
    assert!(!contains(
        &cargo_graph::resolve(&mut inactive, &profile()).unwrap(),
        "nxrs-detector"
    ));

    let mut active = fixture.build();
    active.args(["--target", "x86_64-pc-windows-gnu"]);
    assert_eq!(
        cargo_graph::resolve(&mut active, &profile())
            .unwrap()
            .model_features,
        ["vision/pico-model"]
    );

    let mut configured = fixture.build();
    configured.args(["--config", "build.target=\"x86_64-pc-windows-gnu\""]);
    assert_eq!(
        cargo_graph::resolve(&mut configured, &profile())
            .unwrap()
            .model_features,
        ["vision/pico-model"]
    );

    let mut environment = fixture.build();
    environment.env("CARGO_BUILD_TARGET", "x86_64-pc-windows-gnu");
    assert_eq!(
        cargo_graph::resolve(&mut environment, &profile())
            .unwrap()
            .model_features,
        ["vision/pico-model"]
    );
}

#[test]
fn consumer_workspace_aliases_are_resolved_without_sdk_workspace_assumptions() {
    let fixture = Fixture::new(
        "[dependencies]\nvision={workspace=true,optional=true}\n[features]\ndetect=[\"dep:vision\"]\n",
    );
    fixture.write("consumer/Cargo.toml", "[workspace]\nmembers=[\"app\",\"unrelated\"]\nresolver=\"2\"\n[workspace.dependencies]\nvision={package=\"nxrs-detector\",path=\"../sdk/hal/detector\"}\n");
    fixture.write("consumer/unrelated/Cargo.toml", "[package]\nname=\"unrelated\"\nversion=\"0.1.0\"\nedition=\"2024\"\n[dependencies]\nvision={workspace=true,features=[\"person-model\"]}\n");
    fixture.write("consumer/unrelated/src/lib.rs", "pub fn unrelated() {}\n");
    fixture.lock();
    let mut disabled = fixture.build();
    assert!(!contains(
        &cargo_graph::resolve(&mut disabled, &profile()).unwrap(),
        "nxrs-detector"
    ));
    let mut selected = fixture.build();
    selected.args(["--features", "detect", "-p", "consumer"]);
    assert_eq!(
        cargo_graph::resolve(&mut selected, &profile())
            .unwrap()
            .model_features,
        ["vision?/pico-model"]
    );
    succeeded(&mut selected);
}

#[test]
fn dev_build_and_proc_macro_dependencies_do_not_enable_product_models() {
    let fixture = Fixture::new(
        "[dev-dependencies]\nvision={package=\"nxrs-detector\",path=\"../../sdk/hal/detector\"}\n[build-dependencies]\nbuild-vision={package=\"nxrs-detector\",path=\"../../sdk/hal/detector\"}\n",
    );
    fixture.write("consumer/macros/Cargo.toml", "[package]\nname=\"host-macros\"\nversion=\"0.1.0\"\nedition=\"2024\"\n[lib]\nproc-macro=true\n[dependencies]\nvision={package=\"nxrs-detector\",path=\"../../sdk/hal/detector\"}\n");
    fixture.write("consumer/macros/src/lib.rs", "extern crate proc_macro;\n");
    let manifest = fixture.manifest();
    fs::write(
        &manifest,
        format!(
            "{}\n[dependencies]\nhost-macros={{path=\"../macros\"}}\n",
            fs::read_to_string(&manifest).unwrap()
        ),
    )
    .unwrap();
    fixture.lock();
    let selection = cargo_graph::resolve(&mut fixture.build(), &profile()).unwrap();
    assert!(!contains(&selection, "nxrs-detector"));
    assert!(selection.model_features.is_empty());
}

#[test]
fn transitive_only_model_selection_reports_cargos_direct_dependency_requirement() {
    let fixture = Fixture::new("[dependencies]\ndetection-service={path=\"../../sdk/service\"}\n");
    fixture.lock();
    let mut build = fixture.build();
    let before: Vec<_> = build.get_args().map(ToOwned::to_owned).collect();
    let error = cargo_graph::resolve(&mut build, &profile()).unwrap_err();
    assert!(error.contains("direct application dependency"), "{error}");
    assert_eq!(
        before,
        build.get_args().map(ToOwned::to_owned).collect::<Vec<_>>()
    );
}

#[test]
fn conflicting_manifest_model_fails_before_compilation() {
    let fixture = Fixture::new(
        "[dependencies]\nvision={package=\"nxrs-detector\",path=\"../../sdk/hal/detector\",features=[\"person-model\"]}\n",
    );
    fixture.lock();
    let error = cargo_graph::resolve(&mut fixture.build(), &profile()).unwrap_err();
    assert!(
        error.contains("requires pico-model, but Cargo selected person-model"),
        "{error}"
    );
}

#[cfg(unix)]
#[test]
fn queries_use_the_configured_compiler_environment_and_custom_target() {
    use std::os::unix::fs::PermissionsExt;
    let fixture = Fixture::new(
        "[target.'cfg(target_os = \"nuttx\")'.dependencies]\nvision={package=\"nxrs-detector\",path=\"../../sdk/hal/detector\"}\n",
    );
    fixture.lock();
    let rustc = Command::new("rustup")
        .args(["which", "rustc"])
        .output()
        .unwrap();
    assert!(rustc.status.success());
    let rustc = String::from_utf8(rustc.stdout).unwrap().trim().to_owned();
    // A host-architecture spec avoids needing a target std installation. Its OS
    // differs from the host, so Cargo must query this compiler/spec to choose it.
    let spec = Command::new(&rustc)
        .env("RUSTC_BOOTSTRAP", "1")
        .args([
            "--print",
            "target-spec-json",
            "-Z",
            "unstable-options",
            "--target",
            "x86_64-unknown-linux-gnu",
        ])
        .output()
        .unwrap();
    assert!(
        spec.status.success(),
        "{}",
        String::from_utf8_lossy(&spec.stderr)
    );
    let mut spec: serde_json::Value = serde_json::from_slice(&spec.stdout).unwrap();
    spec["os"] = json!("nuttx");
    fixture.write("custom.json", &serde_json::to_string(&spec).unwrap());
    fixture.write("compiler", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$NXRS_TEST_COMPILER_LOG\"\nexec \"$NXRS_TEST_REAL_RUSTC\" \"$@\"\n");
    let compiler = fixture.0.join("compiler");
    fs::set_permissions(&compiler, fs::Permissions::from_mode(0o755)).unwrap();
    let log = fixture.0.join("compiler.log");
    let mut build = fixture.build();
    build
        .arg("--target")
        .arg(fixture.0.join("custom.json"))
        .env("RUSTC", &compiler)
        .env("CARGO_BUILD_RUSTC", &compiler)
        .env("RUSTC_BOOTSTRAP", "1")
        .env("NXRS_TEST_REAL_RUSTC", rustc)
        .env("NXRS_TEST_COMPILER_LOG", &log);
    assert_eq!(
        cargo_graph::resolve(&mut build, &profile())
            .unwrap()
            .model_features,
        ["vision/pico-model"]
    );
    let invocations = fs::read_to_string(log).unwrap();
    assert!(invocations.contains("custom.json"), "{invocations}");
    assert!(invocations.contains("--print=cfg"), "{invocations}");
}

#[test]
fn an_unknown_frontend_option_cannot_silently_change_the_resolved_graph() {
    let fixture = Fixture::new("");
    fixture.lock();
    let mut build = fixture.build();
    build.arg("--all-targets");
    let error = cargo_graph::resolve(&mut build, &profile()).unwrap_err();
    assert!(error.contains("unsupported Cargo build option"), "{error}");
}

#[test]
fn profile_driver_features_are_additive_and_unused_drivers_leave_the_graph() {
    let fixture = Fixture::new(
        "[dependencies]\ndevices={package=\"nxrs-composition\",path=\"../../sdk/devices\",features=[\"imu\"]}\n\
         model={package=\"nxrs-detector\",path=\"../../sdk/hal/detector\"}\n",
    );
    fixture.write(
        "sdk/Cargo.toml",
        "[workspace]\nmembers=[\"devices\",\"bmi270\",\"lsm6dso\",\"ubx-driver\",\"hal/detector\"]\nresolver=\"2\"\n",
    );
    fixture.write(
        "sdk/devices/Cargo.toml",
        r#"
[package]
name = "nxrs-composition"
version = "0.1.0"
edition = "2021"
[features]
imu = []
gnss = []
[dependencies]
ubx = { package = "nxrs-ubx-driver", path = "../ubx-driver", optional = true }
bmi270 = { package = "nxrs-bmi270", path = "../bmi270", optional = true }
lsm6dso = { package = "nxrs-lsm6dso", path = "../lsm6dso", optional = true }
"#,
    );
    fixture.write("sdk/devices/src/lib.rs", "");
    for chip in ["bmi270", "lsm6dso", "ubx-driver"] {
        fixture.write(
            &format!("sdk/{chip}/Cargo.toml"),
            &format!("[package]\nname=\"nxrs-{chip}\"\nversion=\"0.1.0\"\nedition=\"2021\"\n"),
        );
        fixture.write(&format!("sdk/{chip}/src/lib.rs"), "");
    }
    fixture.lock();
    let sdk = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut profile = Profile::resolve(&sdk, "m5stack-cores3-gnss").unwrap();
    profile.data["hal"]["detector"] = json!("nuttx");
    profile.data["detector"] = json!({"model":"pico", "stack-bytes":16384, "sample-ms":100});
    for chips in [vec!["bmi270"], vec!["lsm6dso"], vec!["bmi270", "lsm6dso"]] {
        // Keep the real driver declarations, replacing only physical graph values.
        let mut data = profile.data.clone();
        data["aliases"]["imu"] = json!("first");
        data["devices"]
            .as_object_mut()
            .unwrap()
            .retain(|_, device| device["bus"] != "sensor_bus");
        for (index, chip) in chips.iter().enumerate() {
            data["devices"][if index == 0 { "first" } else { "second" }] = json!({
                "compatible": if *chip == "bmi270" { "bosch,bmi270" } else { "st,lsm6dso" },
                "bus":"sensor_bus", "address":if *chip == "bmi270" {104} else {106}, "wake":{"kind":"periodic"}
            });
        }
        data["resources"]["sensor_bus"] =
            json!({"kind":"i2c","device":"/dev/i2c0","frequency":400000});
        profile = Profile::from_data_with_catalogs(data, vec![], profile.catalogs().clone());
        let mut build = fixture.build();
        let selection = cargo_graph::resolve(&mut build, &profile).unwrap();
        assert_eq!(selection.model_features, ["model/pico-model"]);
        let (_, _, features) = selection
            .path_packages()
            .find(|(identity, _, _)| identity.starts_with("nxrs-detector v"))
            .unwrap();
        assert!(features.contains("pico-model"));
        assert!(!features.contains("person-model"));
        assert!(!selection
            .packages
            .iter()
            .any(|name| name.starts_with("nxrs-ubx-driver v")));
        for chip in ["bmi270", "lsm6dso"] {
            assert_eq!(
                selection
                    .packages
                    .iter()
                    .any(|name| name.starts_with(&format!("nxrs-{chip} v"))),
                chips.contains(&chip)
            );
        }
        succeeded(&mut build);
    }
    let manifest = fs::read_to_string(fixture.manifest()).unwrap();
    fixture.write(
        "consumer/app/Cargo.toml",
        &manifest.replace("features=[\"imu\"]", "features=[\"gnss\"]"),
    );
    fixture.lock();
    let mut build = fixture.build();
    let gnss = cargo_graph::resolve(&mut build, &profile).unwrap();
    assert_eq!(gnss.driver_features, ["devices/ubx"]);
    assert!(!gnss
        .packages
        .iter()
        .any(|name| name.starts_with("nxrs-bmi270 v") || name.starts_with("nxrs-lsm6dso v")));

    // Board records cannot activate an optional composition dependency.
    fixture.write(
        "consumer/app/Cargo.toml",
        &manifest.replace(
            "path=\"../../sdk/devices\"",
            "path=\"../../sdk/devices\",optional=true",
        ),
    );
    fixture.lock();
    let mut build = fixture.build();
    let unconsumed = cargo_graph::resolve(&mut build, &profile).unwrap();
    assert!(unconsumed.driver_features.is_empty());
    assert!(!unconsumed
        .packages
        .iter()
        .any(|p| p.starts_with("nxrs-composition v")));
    // A missing second feature must not partially modify the original command.
    fixture.write("consumer/app/Cargo.toml", &manifest);
    let catalogue = fs::read_to_string(fixture.0.join("sdk/devices/Cargo.toml")).unwrap();
    fixture.write(
        "sdk/devices/Cargo.toml",
        &catalogue
            .lines()
            .filter(|line| !line.starts_with("lsm6dso = "))
            .collect::<Vec<_>>()
            .join("\n"),
    );
    fixture.lock();
    let mut build = fixture.build();
    let original: Vec<_> = build.get_args().map(|a| a.to_owned()).collect();
    assert!(cargo_graph::resolve(&mut build, &profile).is_err());
    assert_eq!(
        original,
        build.get_args().map(|a| a.to_owned()).collect::<Vec<_>>()
    );
}
