//! Real Cargo witnesses for profile-selected, additive implementation features.
//!
//! The SDK and consumer are separate workspaces. Application source knows only
//! the service; an optional direct HAL alias is the declared composition edge.
use nxrs_build::{
    cargo_graph::{self, Selection},
    profile::Profile,
    TempDir,
};
use nxrs_build_support::bindings::{Catalogs, Package};
use serde_json::{json, Map};
use std::{
    collections::BTreeSet,
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

const MOTION: &str =
    "motion={package=\"nxrs-imu\",path=\"../../sdk/hal/imu\",optional=true,default-features=false}";

struct Fixture(TempDir);

impl Fixture {
    fn new(composition_edge: &str) -> Self {
        let fixture = Self(TempDir::new(&std::env::temp_dir()).unwrap());
        fixture.write(
            "sdk/Cargo.toml",
            "[workspace]\nmembers=[\"hal/imu\",\"service\",\"driver-a\",\"driver-b\",\"driver-default\"]\nresolver=\"2\"\n",
        );
        for (name, value) in [("driver-a", 1), ("driver-b", 2), ("driver-default", 4)] {
            fixture.driver(name, value);
        }
        fixture.write(
            "sdk/hal/imu/Cargo.toml",
            r#"[package]
name="nxrs-imu"
version="0.1.0"
edition="2021"
[package.metadata.nxrs]
kind="hal-capability"
bindings=["bindings/i2c.toml"]
[features]
default=["dep:driver-default"]
driver-a=["dep:driver-a"]
driver-b=["dep:driver-b"]
[dependencies]
driver-a={path="../../driver-a",optional=true}
driver-b={path="../../driver-b",optional=true}
driver-default={path="../../driver-default",optional=true}
"#,
        );
        fixture.write(
            "sdk/hal/imu/bindings/i2c.toml",
            r#"version=1
[[binding]]
compatible="fixture,imu-a"
bus="i2c"
model="bmi270"
addresses=[104,105]
frequencies=[400000]
interrupt=false
cargo_features=["driver-a"]
[[binding]]
compatible="fixture,imu-b"
bus="i2c"
model="lsm6dso"
addresses=[106,107]
frequencies=[400000]
interrupt=false
cargo_features=["driver-b"]
"#,
        );
        fixture.write(
            "sdk/hal/imu/src/lib.rs",
            r#"pub fn mask() -> u8 {
    #[allow(unused_mut)]
    let mut value = 0;
    #[cfg(feature="driver-a")]
    { value |= driver_a::value(); }
    #[cfg(feature="driver-b")]
    { value |= driver_b::value(); }
    #[cfg(feature="default")]
    { value |= driver_default::value(); }
    value
}
"#,
        );
        fixture.write(
            "sdk/service/Cargo.toml",
            r#"[package]
name="navigation-service"
version="0.1.0"
edition="2021"
[dependencies]
internal_motion={package="nxrs-imu",path="../hal/imu",default-features=false}
"#,
        );
        fixture.write(
            "sdk/service/src/lib.rs",
            "pub fn mask() -> u8 { internal_motion::mask() }\n",
        );
        fixture.write(
            "consumer/Cargo.toml",
            "[workspace]\nmembers=[\"app\"]\nresolver=\"2\"\n",
        );
        fixture.application(composition_edge);
        fixture.write(
            "consumer/app/src/main.rs",
            r#"fn main() {
    #[cfg(feature="navigation")]
    println!("{}", nav::mask());
    #[cfg(not(feature="navigation"))]
    println!("off");
}
"#,
        );
        fixture
    }

    fn root(&self) -> &Path {
        &self.0 .0
    }

    fn write(&self, relative: &str, contents: &str) {
        let path = self.root().join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, contents).unwrap();
    }

    fn application(&self, composition_edge: &str) {
        self.write(
            "consumer/app/Cargo.toml",
            &format!(
                r#"[package]
name="consumer"
version="0.1.0"
edition="2021"
[features]
default=["navigation"]
navigation=["dep:nav"]
[dependencies]
nav={{package="navigation-service",path="../../sdk/service",optional=true}}
{composition_edge}
"#,
            ),
        );
    }

    fn driver(&self, name: &str, value: u8) {
        self.write(
            &format!("sdk/{name}/Cargo.toml"),
            &format!("[package]\nname=\"{name}\"\nversion=\"0.1.0\"\nedition=\"2021\"\n"),
        );
        self.write(
            &format!("sdk/{name}/src/lib.rs"),
            &format!("pub fn value() -> u8 {{ {value} }}\n"),
        );
    }

    fn add_gnss(&self, consumed: bool) {
        self.driver("receiver-driver", 8);
        self.write(
            "sdk/hal/gnss/Cargo.toml",
            r#"[package]
name="nxrs-gnss"
version="0.1.0"
edition="2021"
[package.metadata.nxrs]
kind="hal-capability"
bindings=["bindings/uart.toml"]
[features]
receiver=["dep:receiver-driver"]
[dependencies]
receiver-driver={path="../../receiver-driver",optional=true}
"#,
        );
        self.write(
            "sdk/hal/gnss/bindings/uart.toml",
            r#"version=1
[[binding]]
compatible="fixture,receiver"
bus="uart"
model="neo-m9n"
bauds=[9600]
cargo_features=["receiver"]
"#,
        );
        self.write(
            "sdk/hal/gnss/src/lib.rs",
            r#"pub fn mask() -> u8 {
    #[cfg(feature="receiver")]
    { receiver_driver::value() }
    #[cfg(not(feature="receiver"))]
    { 0 }
}
"#,
        );
        if consumed {
            let path = self.root().join("sdk/service/Cargo.toml");
            let manifest = fs::read_to_string(&path).unwrap()
                + "position={package=\"nxrs-gnss\",path=\"../hal/gnss\"}\n";
            fs::write(path, manifest).unwrap();
            self.write(
                "sdk/service/src/lib.rs",
                "pub fn mask() -> u8 { internal_motion::mask() | position::mask() }\n",
            );
        }
    }

    fn other_imu(&self, forwards_to_original: bool) {
        self.write(
            "other-sdk/Cargo.toml",
            "[workspace]\nmembers=[\"hal/imu\"]\nresolver=\"2\"\n",
        );
        let (features, dependencies) = if forwards_to_original {
            (
                "driver-a=[\"original/driver-a\"]",
                "[dependencies]\noriginal={package=\"nxrs-imu\",path=\"../../../sdk/hal/imu\",default-features=false}",
            )
        } else {
            ("driver-a=[]", "")
        };
        self.write(
            "other-sdk/hal/imu/Cargo.toml",
            &format!(
                "[package]\nname=\"nxrs-imu\"\nversion=\"0.2.0\"\nedition=\"2021\"\n[features]\n{features}\n{dependencies}\n"
            ),
        );
        self.write("other-sdk/hal/imu/src/lib.rs", "pub fn other() {}\n");
    }

    fn profile(&self, models: &[char]) -> Profile {
        assert!(!models.is_empty());
        let mut counts = [0, 0];
        let mut devices = Map::new();
        for (index, model) in models.iter().enumerate() {
            let kind = match model {
                'a' => 0,
                'b' => 1,
                _ => panic!("unknown fixture model"),
            };
            let address = [104, 106][kind] + counts[kind];
            counts[kind] += 1;
            devices.insert(
                format!("device_{index}"),
                json!({
                    "compatible":format!("fixture,imu-{model}"),
                    "bus":"sensor_bus",
                    "address":address,
                    "wake":{"kind":"periodic"}
                }),
            );
        }
        let mut data = json!({
            "name":"implementation-fixture",
            "hal":{"imu":"nuttx"},
            "composition":{"version":1,"mode":"hardware"},
            "resources":{"sensor_bus":{"kind":"i2c","device":"/dev/i2c0","frequency":400000}},
            "devices":devices,
            "roles":{"imu":"device_0"}
        });
        let mut catalogs = Catalogs::default();
        catalogs
            .insert(
                "imu",
                Package::load(&self.root().join("sdk/hal/imu")).unwrap(),
            )
            .unwrap();
        let gnss = self.root().join("sdk/hal/gnss");
        if gnss.is_dir() {
            catalogs
                .insert("gnss", Package::load(&gnss).unwrap())
                .unwrap();
            data["hal"]["gnss"] = json!("nuttx");
            data["resources"]["receiver_uart"] =
                json!({"kind":"uart","device":"/dev/ttyS1","baud":9600});
            data["devices"]["receiver"] =
                json!({"compatible":"fixture,receiver","bus":"receiver_uart"});
            data["roles"]["gnss"] = json!("receiver");
        }
        Profile::from_data_with_catalogs(data, vec![], catalogs)
    }

    fn manifest(&self) -> PathBuf {
        self.root().join("consumer/app/Cargo.toml")
    }

    fn command(&self, action: &str) -> Command {
        let mut command = Command::new(env!("CARGO"));
        command
            .current_dir(self.root())
            .args([action, "--offline", "--manifest-path"])
            .arg(self.manifest())
            .env("CARGO_TARGET_DIR", self.root().join("target"));
        for name in [
            "NXRS_PLATFORM",
            "NXRS_RESOLVED_PROFILE",
            "NXRS_SDK_ROOT",
            "CARGO_BUILD_TARGET",
            "RUSTFLAGS",
            "CARGO_ENCODED_RUSTFLAGS",
            "RUSTC",
            "CARGO_BUILD_RUSTC",
            "RUSTC_WRAPPER",
            "RUSTC_WORKSPACE_WRAPPER",
        ] {
            command.env_remove(name);
        }
        command
    }

    fn lock(&self) {
        succeeded(&mut self.command("generate-lockfile"));
    }

    fn build(&self) -> Command {
        let mut command = self.command("build");
        command.args(["--locked", "--bin", "consumer"]);
        command
    }

    fn run(&self, command: &mut Command) -> String {
        succeeded(command);
        let output = succeeded(&mut Command::new(self.executable()));
        String::from_utf8(output.stdout).unwrap().trim().to_owned()
    }

    fn executable(&self) -> PathBuf {
        self.root()
            .join("target/debug")
            .join(format!("consumer{}", std::env::consts::EXE_SUFFIX))
    }
}

fn succeeded(command: &mut Command) -> Output {
    let output = command.output().unwrap();
    assert!(
        output.status.success(),
        "{command:?}\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    output
}

fn contains(selection: &Selection, name: &str) -> bool {
    selection
        .packages
        .iter()
        .any(|package| package.starts_with(&format!("{name} v")))
}

fn command_state(command: &Command) -> (Vec<OsString>, Vec<(OsString, Option<OsString>)>) {
    (
        command.get_args().map(ToOwned::to_owned).collect(),
        command
            .get_envs()
            .map(|(name, value)| (name.to_owned(), value.map(ToOwned::to_owned)))
            .collect(),
    )
}

#[test]
fn detached_service_consumer_uses_a_declared_optional_composition_edge() {
    let fixture = Fixture::new(MOTION);
    fixture.lock();
    let lock = fs::read(fixture.root().join("consumer/Cargo.lock")).unwrap();
    let mut build = fixture.build();
    let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap();
    assert_eq!(selection.implementation_features, ["motion/driver-a"]);
    assert_eq!(
        selection.json()["implementation_features"],
        json!(["motion/driver-a"])
    );
    assert!(selection.model_features.is_empty());
    assert!(contains(&selection, "navigation-service"));
    assert!(contains(&selection, "nxrs-imu"));
    assert!(contains(&selection, "driver-a"));
    assert!(!contains(&selection, "driver-b"));
    assert!(!contains(&selection, "driver-default"));
    assert_eq!(fixture.run(&mut build), "1");
    assert_eq!(
        fs::read(fixture.root().join("consumer/Cargo.lock")).unwrap(),
        lock
    );
}

#[test]
fn preparation_discovers_only_consumed_packages_with_the_final_features() {
    let fixture = Fixture::new(MOTION);
    fixture.add_gnss(false);
    fixture.lock();
    let profile = fixture.profile(&['a', 'b']);
    let mut build = fixture.build();
    let selection = cargo_graph::resolve(&mut build, &profile).unwrap();
    let before = command_state(&build);
    let lock = fs::read(fixture.root().join("consumer/Cargo.lock")).unwrap();
    let consumed = cargo_graph::consumed_hal_packages(&build, &profile).unwrap();
    assert_eq!(consumed.len(), 1);
    assert!(selection.packages.contains(&consumed[0].identity));
    assert!(consumed[0].identity.starts_with("nxrs-imu v0.1.0 ("));
    assert_eq!(
        consumed[0].directory.canonicalize().unwrap(),
        fixture.root().join("sdk/hal/imu").canonicalize().unwrap()
    );
    assert_eq!(
        consumed[0].features,
        BTreeSet::from(["driver-a".to_owned(), "driver-b".to_owned()])
    );
    assert_eq!(command_state(&build), before);
    assert_eq!(
        fs::read(fixture.root().join("consumer/Cargo.lock")).unwrap(),
        lock
    );
    assert_eq!(fixture.run(&mut build), "3");

    let mut disabled = fixture.build();
    disabled.arg("--no-default-features");
    cargo_graph::resolve(&mut disabled, &profile).unwrap();
    assert!(cargo_graph::consumed_hal_packages(&disabled, &profile)
        .unwrap()
        .is_empty());
}

#[test]
fn preparation_uses_the_detached_package_even_without_feature_requests() {
    let fixture = Fixture::new(
        "motion={package=\"nxrs-imu\",path=\"../imu\",optional=true,default-features=false}",
    );
    // The SDK's declarations describe the contract. Cargo consumes a separate
    // package whose manifest has consumer-specific packaging metadata.
    let catalog = fixture.root().join("sdk/hal/imu/bindings/i2c.toml");
    let contents = fs::read_to_string(&catalog)
        .unwrap()
        .replace("cargo_features=[\"driver-a\"]\n", "")
        .replace("cargo_features=[\"driver-b\"]\n", "");
    fs::write(&catalog, &contents).unwrap();
    let profile = fixture.profile(&['a']);
    assert!(profile.implementation_features().unwrap().is_empty());
    fixture.write("consumer/imu/bindings/i2c.toml", &contents);
    fixture.write(
        "consumer/imu/src/lib.rs",
        &fs::read_to_string(fixture.root().join("sdk/hal/imu/src/lib.rs")).unwrap(),
    );
    let manifest = fs::read_to_string(fixture.root().join("sdk/hal/imu/Cargo.toml"))
        .unwrap()
        .replace("../../driver-", "../../sdk/driver-")
        + "\n[package.metadata.packaging]\nfixture=true\n";
    fixture.write("consumer/imu/Cargo.toml", &manifest);
    fixture.write(
        "sdk/service/Cargo.toml",
        &fs::read_to_string(fixture.root().join("sdk/service/Cargo.toml"))
            .unwrap()
            .replace("../hal/imu", "../../consumer/imu"),
    );
    fixture.lock();
    let mut build = fixture.build();
    let selection = cargo_graph::resolve(&mut build, &profile).unwrap();
    assert!(selection.implementation_features.is_empty());
    let before = command_state(&build);
    let consumed = cargo_graph::consumed_hal_packages(&build, &profile).unwrap();
    assert_eq!(consumed.len(), 1);
    assert_eq!(
        consumed[0].directory.canonicalize().unwrap(),
        fixture.root().join("consumer/imu").canonicalize().unwrap()
    );
    assert!(consumed[0].features.is_empty());
    assert_eq!(command_state(&build), before);
    assert_eq!(fixture.run(&mut build), "0");

    // No feature request would have validated these declarations. Preparation
    // must still reject a stale consumed copy instead of using the SDK's bytes.
    fixture.write(
        "consumer/imu/bindings/i2c.toml",
        &(contents + "\n# changed consumer declaration\n"),
    );
    let error = cargo_graph::consumed_hal_packages(&build, &profile).unwrap_err();
    assert!(error.contains("nxrs-imu"), "{error}");
    assert!(
        error.contains("differ from the resolved profile"),
        "{error}"
    );
    assert_eq!(command_state(&build), before);
}

#[test]
fn preparation_rejects_ambiguous_cargo_feature_environment_names() {
    for implicit in [false, true] {
        let fixture = Fixture::new(MOTION);
        let manifest = fixture.root().join("sdk/hal/imu/Cargo.toml");
        let contents = fs::read_to_string(&manifest).unwrap();
        let contents = if implicit {
            // Cargo creates driver_a's feature implicitly; reading only the
            // manifest's explicit [features] table would miss this collision.
            contents + "driver_a={package=\"driver-b\",path=\"../../driver-b\",optional=true}\n"
        } else {
            contents.replace("[dependencies]", "driver_a=[]\n[dependencies]")
        };
        fs::write(manifest, contents).unwrap();
        fixture.lock();
        let profile = fixture.profile(&['a']);
        let mut build = fixture.build();
        let selection = cargo_graph::resolve(&mut build, &profile).unwrap();
        assert_eq!(selection.implementation_features, ["motion/driver-a"]);
        assert!(!contains(&selection, "driver-b"));
        let before = command_state(&build);
        let error = cargo_graph::consumed_hal_packages(&build, &profile).unwrap_err();
        assert!(
            error.contains("prepared feature transport is ambiguous"),
            "{error}"
        );
        assert!(error.contains("CARGO_FEATURE_DRIVER_A"), "{error}");
        assert!(
            error.contains("driver-a") && error.contains("driver_a"),
            "{error}"
        );
        assert_eq!(command_state(&build), before);
        assert!(!fixture.executable().exists());
    }
}

#[test]
fn preparation_features_exclude_the_same_packages_host_build_features() {
    let fixture = Fixture::new(&format!(
        "{MOTION}\n[build-dependencies]\nhost_tool={{package=\"fixture-host-tool\",path=\"../host-tool\"}}"
    ));
    fixture.write(
        "consumer/host-tool/Cargo.toml",
        r#"[package]
name="fixture-host-tool"
version="0.1.0"
edition="2021"
[dependencies]
motion={package="nxrs-imu",path="../../sdk/hal/imu",default-features=false,features=["driver-b"]}
"#,
    );
    fixture.write(
        "consumer/host-tool/src/lib.rs",
        "pub fn mask() -> u8 { motion::mask() }\n",
    );
    fixture.write(
        "consumer/app/build.rs",
        "fn main() { assert_eq!(host_tool::mask(), 2); }\n",
    );
    fixture.lock();
    let profile = fixture.profile(&['a']);
    let rustc = succeeded(Command::new("rustc").arg("-vV"));
    let rustc = String::from_utf8(rustc.stdout).unwrap();
    let target = rustc
        .lines()
        .find_map(|line| line.strip_prefix("host: "))
        .unwrap();
    let mut build = fixture.build();
    build.args(["--target", target]);
    cargo_graph::resolve(&mut build, &profile).unwrap();
    let consumed = cargo_graph::consumed_hal_packages(&build, &profile).unwrap();
    assert_eq!(consumed.len(), 1);
    assert_eq!(
        consumed[0].features,
        BTreeSet::from(["driver-a".to_owned()])
    );
    succeeded(&mut build);
    let executable = fixture
        .root()
        .join("target")
        .join(target)
        .join("debug")
        .join(format!("consumer{}", std::env::consts::EXE_SUFFIX));
    let output = succeeded(&mut Command::new(executable));
    assert_eq!(String::from_utf8(output.stdout).unwrap().trim(), "1");
}

#[test]
fn same_model_requests_deduplicate_and_mixed_models_are_additive() {
    let fixture = Fixture::new(MOTION);
    fixture.lock();
    for (models, features, output) in [
        (vec!['a', 'a'], vec!["motion/driver-a"], "1"),
        (
            vec!['a', 'b'],
            vec!["motion/driver-a", "motion/driver-b"],
            "3",
        ),
        (vec!['a'], vec!["motion/driver-a"], "1"),
    ] {
        let mut build = fixture.build();
        let selection = cargo_graph::resolve(&mut build, &fixture.profile(&models)).unwrap();
        assert_eq!(selection.implementation_features, features);
        assert_eq!(contains(&selection, "driver-b"), output == "3");
        assert_eq!(fixture.run(&mut build), output);
    }
}

#[test]
fn disabled_service_does_not_activate_the_optional_hal_or_its_drivers() {
    let fixture = Fixture::new(MOTION);
    fixture.lock();
    let mut build = fixture.build();
    build.arg("--no-default-features");
    let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap();
    assert!(selection.implementation_features.is_empty());
    for name in [
        "navigation-service",
        "nxrs-imu",
        "driver-a",
        "driver-b",
        "driver-default",
    ] {
        assert!(!contains(&selection, name), "{name}");
    }
    assert_eq!(fixture.run(&mut build), "off");
}

#[test]
fn inherited_aliases_and_user_features_preserve_cargos_additive_defaults() {
    let fixture = Fixture::new("motion={workspace=true,optional=true}");
    fixture.write(
        "consumer/Cargo.toml",
        r#"[workspace]
members=["app","unrelated"]
resolver="2"
[workspace.dependencies]
motion={package="nxrs-imu",path="../sdk/hal/imu",default-features=false}
"#,
    );
    fixture.write(
        "consumer/unrelated/Cargo.toml",
        r#"[package]
name="unrelated"
version="0.1.0"
edition="2021"
[dependencies]
motion={workspace=true,features=["driver-b"]}
"#,
    );
    fixture.write("consumer/unrelated/src/lib.rs", "pub fn unrelated() {}\n");
    fixture.lock();
    let mut build = fixture.build();
    build.args([
        "--no-default-features",
        "--features",
        "navigation",
        "-p",
        "consumer",
    ]);
    let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap();
    assert_eq!(selection.implementation_features, ["motion/driver-a"]);
    assert!(!contains(&selection, "driver-b"));
    assert!(!contains(&selection, "driver-default"));
    assert_eq!(fixture.run(&mut build), "1");

    let mut additive = fixture.build();
    additive.args(["--features", "motion/driver-b"]);
    let selection = cargo_graph::resolve(&mut additive, &fixture.profile(&['a'])).unwrap();
    assert!(contains(&selection, "driver-a"));
    assert!(contains(&selection, "driver-b"));
    assert_eq!(fixture.run(&mut additive), "3");
}

#[test]
fn an_already_enabled_implementation_needs_no_redundant_feature_request() {
    let fixture = Fixture::new(MOTION);
    fixture.lock();
    let mut build = fixture.build();
    build.args(["--features", "motion/driver-a"]);
    let before = command_state(&build);
    let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap();
    assert!(selection.implementation_features.is_empty());
    assert!(contains(&selection, "driver-a"));
    assert!(!contains(&selection, "driver-b"));
    assert_eq!(command_state(&build), before);
    assert_eq!(fixture.run(&mut build), "1");
}

#[test]
fn declared_dependency_defaults_are_not_overridden_by_the_controller() {
    let fixture =
        Fixture::new("motion={package=\"nxrs-imu\",path=\"../../sdk/hal/imu\",optional=true}");
    fixture.lock();
    let mut build = fixture.build();
    let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap();
    assert!(contains(&selection, "driver-a"));
    assert!(contains(&selection, "driver-default"));
    assert!(!contains(&selection, "driver-b"));
    assert_eq!(fixture.run(&mut build), "5");
}

#[test]
fn pure_transitive_and_target_inactive_routes_fail_without_mutating_the_build() {
    for route in [
        "",
        "[target.'cfg(any())'.dependencies]\nmotion={package=\"nxrs-imu\",path=\"../../sdk/hal/imu\",optional=true,default-features=false}",
    ] {
        let fixture = Fixture::new(route);
        fixture.lock();
        let mut build = fixture.build();
        let before = command_state(&build);
        let error = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap_err();
        assert!(error.contains("nxrs-imu"), "{error}");
        assert!(error.contains("driver-a"), "{error}");
        assert_eq!(command_state(&build), before);
    }
}

#[test]
fn cargo_rejects_an_undeclared_local_feature_without_mutating_the_build() {
    let fixture = Fixture::new(MOTION);
    fixture.lock();
    let catalog = fixture.root().join("sdk/hal/imu/bindings/i2c.toml");
    let original = fs::read_to_string(&catalog).unwrap();
    fs::write(
        catalog,
        original.replace(
            "cargo_features=[\"driver-a\"]",
            "cargo_features=[\"undeclared-driver\"]",
        ),
    )
    .unwrap();
    let mut build = fixture.build();
    let before = command_state(&build);
    let error = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap_err();
    assert!(error.contains("undeclared-driver"), "{error}");
    assert!(error.contains("feature"), "{error}");
    assert_eq!(command_state(&build), before);
    assert!(!fixture.executable().exists());
}

#[test]
fn changed_package_declarations_fail_even_when_the_required_feature_is_enabled() {
    for already_enabled in [false, true] {
        let fixture = Fixture::new(MOTION);
        fixture.lock();
        let profile = fixture.profile(&['a']);
        let catalog = fixture.root().join("sdk/hal/imu/bindings/i2c.toml");
        let original = fs::read_to_string(&catalog).unwrap();
        fs::write(
            catalog,
            original.replace(
                "cargo_features=[\"driver-a\"]",
                "cargo_features=[\"driver-b\"]",
            ),
        )
        .unwrap();
        let mut build = fixture.build();
        if already_enabled {
            build.args(["--features", "motion/driver-a"]);
        }
        let before = command_state(&build);
        let error = cargo_graph::resolve(&mut build, &profile).unwrap_err();
        assert!(error.contains("nxrs-imu"), "{error}");
        assert_eq!(command_state(&build), before);
    }
}

#[test]
fn packaging_only_manifest_edits_preserve_the_declared_catalog_contract() {
    let fixture = Fixture::new(MOTION);
    let manifest = fixture.root().join("sdk/hal/imu/Cargo.toml");
    let original = fs::read_to_string(&manifest).unwrap();
    fs::write(
        &manifest,
        format!("{original}\n[dev-dependencies]\ndriver-b={{path=\"../../driver-b\"}}\n"),
    )
    .unwrap();
    let profile = fixture.profile(&['a']);
    // Detached consumers omit dependency-owned tests and may add packaging
    // metadata. Neither change alters the declared binding/configuration bytes.
    fs::write(
        &manifest,
        format!("{original}\n[package.metadata.packaging]\nfixture=true\n"),
    )
    .unwrap();
    fixture.lock();
    let mut build = fixture.build();
    let selection = cargo_graph::resolve(&mut build, &profile).unwrap();
    assert_eq!(selection.implementation_features, ["motion/driver-a"]);
    assert!(!contains(&selection, "driver-b"));
    assert_eq!(fixture.run(&mut build), "1");
}

#[test]
fn multiple_consumed_hal_identities_cannot_share_one_profile_feature_request() {
    let fixture = Fixture::new(&format!(
        "{MOTION}\nother_motion={{package=\"nxrs-imu\",path=\"../../other-sdk/hal/imu\",default-features=false}}"
    ));
    fixture.other_imu(false);
    fixture.lock();
    let tree = succeeded(fixture.command("tree").args(["--edges", "normal"]));
    let tree = String::from_utf8(tree.stdout).unwrap();
    assert!(tree.contains("nxrs-imu v0.1.0"), "{tree}");
    assert!(tree.contains("nxrs-imu v0.2.0"), "{tree}");
    let mut build = fixture.build();
    let before = command_state(&build);
    let error = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap_err();
    assert!(error.contains("nxrs-imu"), "{error}");
    assert!(error.contains("multiple"), "{error}");
    assert_eq!(command_state(&build), before);
    let error = cargo_graph::consumed_hal_packages(&build, &fixture.profile(&['a'])).unwrap_err();
    assert!(error.contains("multiple nxrs-imu packages"), "{error}");
    assert_eq!(command_state(&build), before);
}

#[test]
fn a_composition_edge_cannot_introduce_a_second_hal_identity_by_forwarding() {
    let fixture = Fixture::new(
        "motion={package=\"nxrs-imu\",path=\"../../other-sdk/hal/imu\",optional=true,default-features=false}",
    );
    fixture.other_imu(true);
    fixture.lock();
    // Cargo accepts this edge and enables the original package's driver through
    // the second version. Satisfying the feature alone is therefore insufficient.
    let tree = succeeded(fixture.command("tree").args([
        "--features",
        "motion/driver-a",
        "--edges",
        "normal",
    ]));
    let tree = String::from_utf8(tree.stdout).unwrap();
    assert!(tree.contains("nxrs-imu v0.1.0"), "{tree}");
    assert!(tree.contains("nxrs-imu v0.2.0"), "{tree}");
    assert!(tree.contains("driver-a v0.1.0"), "{tree}");
    let mut build = fixture.build();
    let before = command_state(&build);
    let error = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap_err();
    assert!(error.contains("nxrs-imu"), "{error}");
    assert_eq!(command_state(&build), before);
}

#[test]
fn a_later_missing_feature_route_cannot_leave_an_earlier_request_on_the_command() {
    let fixture =
        Fixture::new("position={package=\"nxrs-gnss\",path=\"../../sdk/hal/gnss\",optional=true}");
    fixture.add_gnss(true);
    fixture.lock();
    let mut build = fixture.build();
    let before = command_state(&build);
    let error = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap_err();
    assert!(error.contains("nxrs-imu"), "{error}");
    assert_eq!(command_state(&build), before);
}

#[test]
fn a_generic_failure_also_preserves_a_pending_detector_feature_request() {
    let fixture =
        Fixture::new("vision={package=\"nxrs-detector\",path=\"../../sdk/hal/detector\"}");
    fixture.write(
        "sdk/hal/detector/Cargo.toml",
        "[package]\nname=\"nxrs-detector\"\nversion=\"0.1.0\"\nedition=\"2021\"\n[features]\npico-model=[]\nperson-model=[]\n",
    );
    fixture.write("sdk/hal/detector/src/lib.rs", "pub fn detector() {}\n");
    // This HAL is a direct external dependency, with a legitimate model route.
    let sdk = fixture.root().join("sdk/Cargo.toml");
    let manifest = fs::read_to_string(&sdk).unwrap();
    fs::write(
        sdk,
        manifest.replace("\"hal/imu\"", "\"hal/imu\",\"hal/detector\""),
    )
    .unwrap();
    fixture.lock();
    let mut profile = fixture.profile(&['a']);
    profile.data["hal"]["detector"] = json!("nuttx");
    profile.data["detector"] = json!({"model":"pico","stack-bytes":16384,"sample-ms":100});
    let mut build = fixture.build();
    let before = command_state(&build);
    let error = cargo_graph::resolve(&mut build, &profile).unwrap_err();
    assert!(error.contains("nxrs-imu"), "{error}");
    assert_eq!(command_state(&build), before);
}

#[test]
fn a_conflicting_optional_alias_does_not_prevent_a_valid_composition_route() {
    let fixture = Fixture::new(
        r#"a_default={package="nxrs-imu",path="../../sdk/hal/imu",optional=true}
z_minimal={package="nxrs-imu",path="../../sdk/hal/imu",optional=true,default-features=false}
vision={package="nxrs-detector",path="../../sdk/hal/detector"}
"#,
    );
    fixture.write(
        "sdk/hal/detector/Cargo.toml",
        "[package]\nname=\"nxrs-detector\"\nversion=\"0.1.0\"\nedition=\"2021\"\n[features]\npico-model=[]\nperson-model=[]\n",
    );
    fixture.write(
        "sdk/hal/detector/src/lib.rs",
        r#"#[cfg(not(feature="pico-model"))]
compile_error!("the product must select its pico model");
#[cfg(feature="person-model")]
compile_error!("the alternate composition edge selected the wrong model");
pub fn detector() {}
"#,
    );
    let manifest = fixture.root().join("sdk/hal/imu/Cargo.toml");
    let original = fs::read_to_string(&manifest).unwrap();
    fs::write(
        manifest,
        original.replace(
            "default=[\"dep:driver-default\"]",
            "default=[\"dep:driver-default\",\"detector/person-model\"]",
        ) + "detector={package=\"nxrs-detector\",path=\"../detector\",optional=true}\n",
    )
    .unwrap();
    fixture.lock();
    let mut profile = fixture.profile(&['a']);
    profile.data["hal"]["detector"] = json!("nuttx");
    profile.data["detector"] = json!({"model":"pico","stack-bytes":16384,"sample-ms":100});

    // The first alias is a valid Cargo edge, but its declared defaults add a
    // conflicting detector model. The second alias must be tried independently.
    let trial = succeeded(fixture.command("tree").args([
        "--features",
        "vision/pico-model,a_default/driver-a",
        "--edges",
        "normal",
        "--format",
        "{p} {f}",
    ]));
    let trial = String::from_utf8(trial.stdout).unwrap();
    assert!(
        trial.lines().any(|line| line.contains("nxrs-detector")
            && line.contains("pico-model")
            && line.contains("person-model")),
        "{trial}"
    );

    let mut build = fixture.build();
    let selection = cargo_graph::resolve(&mut build, &profile).unwrap();
    assert_eq!(selection.model_features, ["vision/pico-model"]);
    assert_eq!(selection.implementation_features, ["z_minimal/driver-a"]);
    assert!(contains(&selection, "driver-a"));
    assert!(!contains(&selection, "driver-b"));
    assert!(!contains(&selection, "driver-default"));
    assert_eq!(fixture.run(&mut build), "1");
}

#[test]
fn unconsumed_profile_devices_do_not_require_a_feature_route_or_add_drivers() {
    let fixture = Fixture::new(MOTION);
    fixture.add_gnss(false);
    fixture.lock();
    let mut build = fixture.build();
    let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap();
    assert_eq!(selection.implementation_features, ["motion/driver-a"]);
    assert!(!contains(&selection, "nxrs-gnss"));
    assert!(!contains(&selection, "receiver-driver"));
    assert_eq!(fixture.run(&mut build), "1");
}

#[test]
fn dev_build_and_proc_macro_uses_do_not_become_product_implementation_requests() {
    let fixture = Fixture::new(
        r#"host-macros={path="../macros"}
[dev-dependencies]
host_motion={package="nxrs-imu",path="../../sdk/hal/imu",default-features=false}
[build-dependencies]
host_motion={package="nxrs-imu",path="../../sdk/hal/imu",default-features=false}
"#,
    );
    fixture.write("consumer/app/build.rs", "fn main() {}\n");
    fixture.write(
        "consumer/macros/Cargo.toml",
        r#"[package]
name="host-macros"
version="0.1.0"
edition="2021"
[lib]
proc-macro=true
[dependencies]
host_motion={package="nxrs-imu",path="../../sdk/hal/imu",default-features=false}
"#,
    );
    fixture.write("consumer/macros/src/lib.rs", "extern crate proc_macro;\n");
    fixture.lock();
    let mut build = fixture.build();
    build.arg("--no-default-features");
    let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap();
    assert!(selection.implementation_features.is_empty());
    for name in ["nxrs-imu", "driver-a", "driver-b", "driver-default"] {
        assert!(!contains(&selection, name), "{name}");
    }
    assert!(
        cargo_graph::consumed_hal_packages(&build, &fixture.profile(&['a']))
            .unwrap()
            .is_empty()
    );
    assert_eq!(fixture.run(&mut build), "off");
}

#[test]
fn declaration_lookup_does_not_fetch_unselected_registry_packages() {
    for unselected in ["target", "dev", "workspace"] {
        let fixture = Fixture::new(MOTION);
        // A registry index is enough to resolve this package into the lockfile.
        // Its archive deliberately does not exist: the production graph must
        // never need it, including while locating a consumed HAL's declarations.
        fixture.write(
            "registry/index/un/se/unselected-dependency",
            &(json!({
                "name":"unselected-dependency", "vers":"1.0.0", "deps":[],
                "cksum":"0".repeat(64), "features":{}, "yanked":false,
            })
            .to_string()
                + "\n"),
        );
        let dependency = "unselected-dependency=\"=1.0.0\"\n";
        if unselected == "workspace" {
            fixture.write(
                "consumer/Cargo.toml",
                "[workspace]\nmembers=[\"app\",\"unrelated\"]\nresolver=\"2\"\n",
            );
            fixture.write(
                "consumer/unrelated/Cargo.toml",
                &format!(
                    "[package]\nname=\"unrelated\"\nversion=\"0.1.0\"\nedition=\"2021\"\n[dependencies]\n{dependency}"
                ),
            );
            fixture.write("consumer/unrelated/src/lib.rs", "pub fn unused() {}\n");
        } else {
            let section = if unselected == "target" {
                "[target.'cfg(any())'.dependencies]"
            } else {
                "[dev-dependencies]"
            };
            let manifest = fixture.manifest();
            fs::write(
                &manifest,
                format!(
                    "{}\n{section}\n{dependency}",
                    fs::read_to_string(&manifest).unwrap()
                ),
            )
            .unwrap();
        }
        let registry = format!(
            "source.fixture-registry.local-registry={}",
            json!(fixture.root().join("registry"))
        );
        let configuration = [
            "--config",
            "source.crates-io.replace-with=\"fixture-registry\"",
            "--config",
            registry.as_str(),
        ];
        succeeded(fixture.command("generate-lockfile").args(configuration));

        let mut baseline = fixture.build();
        baseline.args(configuration);
        assert_eq!(fixture.run(&mut baseline), "0", "{unselected}");

        let mut build = fixture.build();
        build.args(configuration);
        let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a']))
            .unwrap_or_else(|error| panic!("{unselected}: {error}"));
        assert_eq!(selection.implementation_features, ["motion/driver-a"]);
        assert!(!contains(&selection, "unselected-dependency"));
        let consumed = cargo_graph::consumed_hal_packages(&build, &fixture.profile(&['a']))
            .unwrap_or_else(|error| panic!("{unselected}: {error}"));
        assert_eq!(consumed.len(), 1);
        assert_eq!(
            consumed[0].features,
            BTreeSet::from(["driver-a".to_owned()])
        );
        assert_eq!(fixture.run(&mut build), "1", "{unselected}");
    }
}

#[test]
fn cargo_verifies_declarations_when_the_package_path_contains_spaces_and_parentheses() {
    let mut fixture = Fixture::new(MOTION);
    let directory = fixture.root().with_file_name(format!(
        "{} (local SDK)",
        fixture.root().file_name().unwrap().to_string_lossy()
    ));
    fs::rename(fixture.root(), &directory).unwrap();
    fixture.0 .0 = directory;
    fixture.lock();
    let mut build = fixture.build();
    let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap();
    assert_eq!(selection.implementation_features, ["motion/driver-a"]);
    assert!(selection
        .packages
        .iter()
        .any(|package| { package.starts_with("nxrs-imu v") && package.contains(" (local SDK)") }));
    let consumed = cargo_graph::consumed_hal_packages(&build, &fixture.profile(&['a'])).unwrap();
    assert_eq!(consumed.len(), 1);
    assert_eq!(
        consumed[0].directory.canonicalize().unwrap(),
        fixture.root().join("sdk/hal/imu").canonicalize().unwrap()
    );
    assert_eq!(fixture.run(&mut build), "1");
}

#[cfg(unix)]
#[test]
fn declaration_lookup_preserves_cargos_symlinked_source_identity() {
    let mut fixture = Fixture::new(MOTION);
    let aliases = TempDir::new(&std::env::temp_dir()).unwrap();
    let alias = aliases.0.join("linked SDK workspace");
    std::os::unix::fs::symlink(fixture.root(), &alias).unwrap();
    // Keep ownership of the real tree as well as the temporary alias directory.
    // The query must use the lexical path Cargo saw, even when canonicalizing
    // that path reaches another spelling of the same files (macOS /var does).
    let _real_directory = std::mem::replace(&mut fixture.0, TempDir(alias.clone()));
    fixture.lock();
    let tree = succeeded(fixture.command("tree").args([
        "--edges",
        "normal,no-proc-macro",
        "--format",
        "{p}",
    ]));
    let tree = String::from_utf8(tree.stdout).unwrap();
    assert!(
        tree.contains(alias.join("sdk/hal/imu").to_str().unwrap()),
        "{tree}"
    );
    let mut build = fixture.build();
    let selection = cargo_graph::resolve(&mut build, &fixture.profile(&['a'])).unwrap();
    assert_eq!(selection.implementation_features, ["motion/driver-a"]);
    let consumed = cargo_graph::consumed_hal_packages(&build, &fixture.profile(&['a'])).unwrap();
    assert_eq!(consumed.len(), 1);
    assert_eq!(consumed[0].directory, alias.join("sdk/hal/imu"));
    assert_eq!(fixture.run(&mut build), "1");
}
