//! Detached Cargo witnesses for controller preparation and package-local transport.
//!
//! Host fixtures exercise generated simulation input and an ordinary C helper.
//! The production sensor schemas are rendered separately for their actual target;
//! compiling their values on the host is not a NuttX ABI or device qualification.
use nxrs_build::{build, profile::Profile, TempDir};
use nxrs_build_support::{handoff, resolved};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
    time::SystemTime,
};

struct Fixture {
    temporary: TempDir,
    capability: &'static str,
    target: String,
}

impl Fixture {
    fn new(capability: &'static str) -> Self {
        let temporary = TempDir::new(&std::env::temp_dir()).unwrap();
        let version = nxrs_build::output(Command::new(env!("CARGO")).arg("-vV")).unwrap();
        let fixture = Self {
            temporary,
            capability,
            target: version
                .lines()
                .find_map(|line| line.strip_prefix("host: "))
                .unwrap()
                .to_owned(),
        };
        fixture.write(
            "sdk/Cargo.toml",
            &format!("[workspace]\nmembers=[\"hal/{capability}\",\"service\"]\nresolver=\"2\"\n"),
        );
        fs::create_dir_all(fixture.sdk().join("platform/profiles")).unwrap();
        fs::create_dir_all(fixture.sdk().join("platform/nuttx/platforms")).unwrap();
        fs::create_dir_all(fixture.out()).unwrap();
        fixture.write(
            "sdk/service/Cargo.toml",
            &format!(
                "[package]\nname=\"portable-service\"\nversion=\"0.0.0\"\nedition=\"2021\"\n\
                 [dependencies]\nsource={{package=\"nxrs-{capability}\",path=\"../hal/{capability}\",default-features=false}}\n"
            ),
        );
        fixture.write(
            "sdk/service/src/lib.rs",
            "pub fn value() -> u64 { source::value() }\n",
        );
        fixture.write(
            "consumer/Cargo.toml",
            "[workspace]\nmembers=[\"app\"]\nresolver=\"2\"\n",
        );
        fixture.write(
            "consumer/app/Cargo.toml",
            &format!(
                "[package]\nname=\"prepared-consumer\"\nversion=\"0.0.0\"\nedition=\"2021\"\n\
                 [features]\ndefault=[]\nhelper=[\"motion/native\"]\n\
                 [dependencies]\nservice={{package=\"portable-service\",path=\"../../sdk/service\"}}\n\
                 motion={{package=\"nxrs-{capability}\",path=\"../../sdk/hal/{capability}\",optional=true,default-features=false}}\n"
            ),
        );
        fixture.write(
            "consumer/app/src/main.rs",
            "fn main() { println!(\"{}\", service::value()); }\n",
        );
        fixture.write(
            &format!("sdk/hal/{capability}/build.rs"),
            r#"
fn main() {
    nxrs_build_support::configure_hal().unwrap();
    // Instrument the real shared adapter without implementing any preparation.
    use std::io::Write;
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    writeln!(std::fs::OpenOptions::new().create(true).append(true)
        .open(out.join("adapter-runs")).unwrap(), "run").unwrap();
}
"#,
        );
        fixture
    }

    fn simulation() -> Self {
        let fixture = Self::new("imu");
        fixture.write(
            "sdk/hal/imu/Cargo.toml",
            &format!(
                "[package]\nname=\"nxrs-imu\"\nversion=\"0.0.0\"\nedition=\"2021\"\n\
                 [package.metadata.nxrs]\nkind=\"hal-capability\"\nplatforms=[\"mock\",\"nuttx\"]\nbindings=[\"bindings/i2c.toml\"]\n\
                 [package.metadata.nxrs.configuration]\nnuttx=\"bindings/nuttx.toml\"\n\
                 [features]\ndefault=[]\nprobe=[]\nnative=[]\n\
                 [build-dependencies]\nnxrs-build-support={{path={:?}}}\n",
                support()
            ),
        );
        for name in ["i2c.toml", "nuttx.toml"] {
            fixture.copy_binding("imu", name);
        }
        fixture.write(
            "sdk/hal/imu/src/lib.rs",
            r#"
#[cfg(hal_provider="simulation")]
mod selected {
    mod simulation { pub enum Source { Imu, Gnss } }
    include!(concat!(env!("OUT_DIR"), "/simulation_config.rs"));
    pub fn value() -> u64 {
        assert_eq!(SIM_SOURCES.len(), 1);
        SIM_SEED
    }
}
#[cfg(not(hal_provider="simulation"))]
mod selected { pub fn value() -> u64 { 0 } }
pub use selected::value;
"#,
        );
        for (name, seed) in [("first", 111), ("second", 222)] {
            fixture.write(
                &format!("sdk/platform/profiles/{name}.toml"),
                &format!(
                    "execution=\"std\"\ntarget-os=[{:?}]\n\
                     [composition]\nversion=1\nmode=\"simulation\"\n\
                     [simulation]\nseed={seed}\nendpoints=[\"imu\"]\n\
                     [hal]\nimu=\"simulation\"\n",
                    std::env::consts::OS
                ),
            );
        }
        fixture
    }

    fn root(&self) -> &Path {
        &self.temporary.0
    }

    fn sdk(&self) -> PathBuf {
        self.root().join("sdk")
    }

    fn out(&self) -> PathBuf {
        self.root().join("prepared output")
    }

    fn hal(&self) -> PathBuf {
        self.sdk().join("hal").join(self.capability)
    }

    fn write(&self, relative: &str, contents: &str) {
        let path = self.root().join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, contents).unwrap();
    }

    fn copy_binding(&self, capability: &str, name: &str) {
        let path = self
            .sdk()
            .join("hal")
            .join(capability)
            .join("bindings")
            .join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::copy(
            source()
                .join("hal")
                .join(capability)
                .join("bindings")
                .join(name),
            path,
        )
        .unwrap();
    }

    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO"));
        command
            .current_dir(self.root().join("consumer"))
            .args(["build", "--offline", "--manifest-path"])
            .arg(self.root().join("consumer/app/Cargo.toml"))
            .args(["--target", &self.target, "--message-format=json"])
            .env("CARGO_TARGET_DIR", self.root().join("cargo output"));
        for variable in [
            "CARGO_BUILD_TARGET",
            "RUSTFLAGS",
            "CARGO_ENCODED_RUSTFLAGS",
            "NXRS_PLATFORM",
            "NXRS_SDK_ROOT",
            "NXRS_RESOLVED_PROFILE",
            "NXRS_PREPARED_BUILD",
            "NXRS_NUTTX_NATIVE_CONFIG",
            "NXRS_NUTTX_INCLUDE",
        ] {
            command.env_remove(variable);
        }
        command
    }

    fn prepare(&self, name: &str, command: &mut Command) -> PathBuf {
        let profile = Profile::resolve(&self.sdk(), name).unwrap();
        let selection = build::prepare_cargo(
            command,
            &self.out(),
            &self.sdk(),
            &profile,
            &self.target,
            std::env::consts::OS,
        )
        .unwrap();
        assert!(selection
            .packages
            .iter()
            .any(|package| { package.starts_with(&format!("nxrs-{} v", self.capability)) }));
        assert!(!selection
            .packages
            .iter()
            .any(|package| package.starts_with("nxrs-ble v")));
        PathBuf::from(environment(command, handoff::ENVIRONMENT))
    }

    fn value(&self) -> String {
        let binary = self
            .root()
            .join("cargo output")
            .join(&self.target)
            .join("debug")
            .join(format!("prepared-consumer{}", std::env::consts::EXE_SUFFIX));
        let output = Command::new(binary).output().unwrap();
        success(&output);
        String::from_utf8(output.stdout).unwrap().trim().into()
    }
}

fn source() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

fn support() -> PathBuf {
    source().join("platform/build-support")
}

fn environment(command: &Command, name: &str) -> OsString {
    command
        .get_envs()
        .find_map(|(key, value)| (key == name).then_some(value).flatten())
        .unwrap_or_else(|| panic!("missing {name}"))
        .to_owned()
}

fn action(command: &Command, action: &str) -> Command {
    let mut result = Command::new(command.get_program());
    result.arg(action).args(command.get_args().skip(1));
    if let Some(directory) = command.get_current_dir() {
        result.current_dir(directory);
    }
    for (name, value) in command.get_envs() {
        if let Some(value) = value {
            result.env(name, value);
        } else {
            result.env_remove(name);
        }
    }
    result
}

fn success(output: &Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn failure(output: &Output, message: &str) {
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(!output.status.success(), "unexpected success: {stderr}");
    assert!(stderr.contains(message), "expected {message:?}: {stderr}");
}

fn script_output(output: &Output, package: &str) -> PathBuf {
    success(output);
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find(|message| {
            message["reason"] == "build-script-executed"
                && message["package_id"]
                    .as_str()
                    .is_some_and(|id| id.contains(package))
        })
        .map(|message| PathBuf::from(message["out_dir"].as_str().unwrap()))
        .unwrap_or_else(|| panic!("no build-script output for {package}"))
}

fn outputs(receipt: &Path) -> Vec<PathBuf> {
    let receipt: Value = serde_json::from_slice(&fs::read(receipt).unwrap()).unwrap();
    receipt["packages"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|package| {
            let record: Value = serde_json::from_slice(
                &fs::read(package["record"]["path"].as_str().unwrap()).unwrap(),
            )
            .unwrap();
            record["outputs"]
                .as_array()
                .unwrap()
                .iter()
                .map(|output| PathBuf::from(output["path"].as_str().unwrap()))
                .collect::<Vec<_>>()
        })
        .collect()
}

fn output_named(receipt: &Path, name: &str) -> PathBuf {
    outputs(receipt)
        .into_iter()
        .find(|path| path.file_name().is_some_and(|file| file == name))
        .unwrap_or_else(|| panic!("preparation has no {name}"))
}

fn stamps(paths: impl IntoIterator<Item = PathBuf>) -> BTreeMap<PathBuf, SystemTime> {
    paths
        .into_iter()
        .map(|path| {
            let modified = fs::metadata(&path).unwrap().modified().unwrap();
            (path, modified)
        })
        .collect()
}

#[test]
fn prepared_cargo_modes_are_fresh_and_profile_switches_do_not_reuse_the_last_product() {
    let fixture = Fixture::simulation();
    let mut first = fixture.command();
    let first_receipt = fixture.prepare("first", &mut first);
    let generated = output_named(&first_receipt, "simulation_config.rs");
    assert!(fs::read_to_string(&generated).unwrap().contains("111"));
    let before = stamps(
        outputs(&first_receipt)
            .into_iter()
            .chain([first_receipt.clone()]),
    );

    for mode in ["build", "check", "test", "doc"] {
        let initial = action(&first, mode).output().unwrap();
        let out = script_output(&initial, "nxrs-imu");
        let runs = fs::read(out.join("adapter-runs")).unwrap();
        let delivered = stamps([out.join("simulation_config.rs")]);
        let again = action(&first, mode).output().unwrap();
        success(&again);
        assert_eq!(
            fs::read(out.join("adapter-runs")).unwrap(),
            runs,
            "{mode} reran a fresh adapter: {}",
            String::from_utf8_lossy(&again.stderr)
        );
        assert_eq!(stamps([out.join("simulation_config.rs")]), delivered);
    }
    assert_eq!(fixture.value(), "111");

    // Cargo must revisit the transport when its OUT_DIR copy is edited or
    // deleted, including an otherwise fresh rustdoc invocation. Only the
    // verified immutable artifact is authoritative.
    let warm = action(&first, "build").output().unwrap();
    let out = script_output(&warm, "nxrs-imu");
    let delivered = out.join("simulation_config.rs");
    let expected = fs::read(&generated).unwrap();
    fs::write(&delivered, "const SIM_SEED: u64 = 999;\n").unwrap();
    success(&action(&first, "build").output().unwrap());
    assert_eq!(fs::read(&delivered).unwrap(), expected);
    assert_eq!(fixture.value(), "111");
    fs::remove_file(&delivered).unwrap();
    success(&action(&first, "doc").output().unwrap());
    assert_eq!(fs::read(&delivered).unwrap(), expected);
    let runs = fs::read(out.join("adapter-runs")).unwrap();
    success(&action(&first, "doc").output().unwrap());
    assert_eq!(fs::read(out.join("adapter-runs")).unwrap(), runs);

    let mut same = fixture.command();
    assert_eq!(fixture.prepare("first", &mut same), first_receipt);
    assert_eq!(stamps(before.keys().cloned()), before);

    let mut second = fixture.command();
    let second_receipt = fixture.prepare("second", &mut second);
    assert_ne!(second_receipt, first_receipt);
    // Preparation B must not overwrite the explicitly retained A selection.
    success(&action(&first, "build").output().unwrap());
    assert_eq!(fixture.value(), "111");
    success(&action(&second, "build").output().unwrap());
    assert_eq!(fixture.value(), "222");
    success(&action(&first, "build").output().unwrap());
    assert_eq!(fixture.value(), "111");

    let plain = action(&fixture.command(), "build").output().unwrap();
    let unbound_out = script_output(&plain, "nxrs-imu");
    assert_eq!(fixture.value(), "0");
    assert!(!unbound_out.join("simulation_config.rs").exists());
    success(&action(&first, "build").output().unwrap());
    assert_eq!(fixture.value(), "111");
}

#[test]
fn explicit_preparation_rejects_stale_missing_corrupt_and_wrong_feature_inputs() {
    let fixture = Fixture::simulation();
    let mut command = fixture.command();
    let receipt = fixture.prepare("first", &mut command);
    success(&action(&command, "build").output().unwrap());

    let schema = fixture.hal().join("bindings/nuttx.toml");
    let mut contents = fs::read_to_string(&schema).unwrap();
    contents.push_str("\n# same-path preparation freshness\n");
    fs::write(schema, contents).unwrap();
    failure(&action(&command, "build").output().unwrap(), "stale");
    let mut refreshed = fixture.command();
    let refreshed_receipt = fixture.prepare("first", &mut refreshed);
    assert_ne!(refreshed_receipt, receipt);
    success(&action(&refreshed, "build").output().unwrap());

    let mut missing = action(&refreshed, "build");
    missing.env(
        handoff::ENVIRONMENT,
        fixture.out().join("missing-preparation.json"),
    );
    failure(&missing.output().unwrap(), "missing-preparation.json");

    let generated = output_named(&refreshed_receipt, "simulation_config.rs");
    let original = fs::read(&generated).unwrap();
    fs::write(&generated, "const SIM_SEED: u64 = 999;\n").unwrap();
    failure(
        &action(&refreshed, "build").output().unwrap(),
        "prepared output was modified",
    );
    fs::write(&generated, &original).unwrap();
    success(&action(&refreshed, "build").output().unwrap());
    fs::remove_file(&generated).unwrap();
    failure(
        &action(&refreshed, "build").output().unwrap(),
        "simulation_config.rs",
    );
    fs::write(&generated, original).unwrap();

    let mut wrong_features = action(&refreshed, "build");
    wrong_features.args(["--features", "motion/probe"]);
    failure(&wrong_features.output().unwrap(), "prepared features");
    let mut qualified_features = fixture.command();
    qualified_features.args(["--features", "motion/probe"]);
    fixture.prepare("first", &mut qualified_features);
    success(&qualified_features.output().unwrap());
}

#[test]
fn prepared_target_features_do_not_admit_a_different_host_dependency_unit() {
    let fixture = Fixture::simulation();
    let manifest = fixture.root().join("consumer/app/Cargo.toml");
    let contents = fs::read_to_string(&manifest).unwrap()
        + "\n[build-dependencies]\ncompiler_motion={package=\"nxrs-imu\",path=\"../../sdk/hal/imu\",default-features=false,features=[\"probe\"]}\n";
    fs::write(manifest, contents).unwrap();
    fixture.write("consumer/app/build.rs", "fn main() {}\n");
    let mut command = fixture.command();
    fixture.prepare("first", &mut command);
    // Resolver 2 builds the host dependency with probe and the target dependency
    // without it, even though their target triples happen to be the same.
    failure(&command.output().unwrap(), "prepared features");
}

#[test]
fn production_sensor_schemas_are_prepared_and_compile_without_cargo_generation() {
    let fixture = Fixture::simulation();
    fixture.write(
        "sdk/hal/gnss/Cargo.toml",
        "[package]\nname=\"nxrs-gnss\"\nversion=\"0.0.0\"\n\
         [package.metadata.nxrs]\nplatforms=[\"nuttx\"]\nbindings=[\"bindings/uart.toml\"]\n\
         [package.metadata.nxrs.configuration]\nnuttx=\"bindings/nuttx.toml\"\n",
    );
    for name in ["uart.toml", "nuttx.toml"] {
        fixture.copy_binding("gnss", name);
    }
    fs::copy(
        source().join("platform/nuttx/platforms/m5stack-cores3-gnss.toml"),
        fixture
            .sdk()
            .join("platform/nuttx/platforms/m5stack-cores3-gnss.toml"),
    )
    .unwrap();
    let profile = Profile::resolve(&fixture.sdk(), "m5stack-cores3-gnss").unwrap();
    let target = "xtensa-esp32s3-nuttx";
    let profile_receipt = fixture.out().join("physical-profile.json");
    resolved::write(&profile_receipt, &fixture.sdk(), &profile, target, "nuttx").unwrap();
    let packages = ["imu", "gnss"].map(|capability| {
        let directory = fixture.sdk().join("hal").join(capability);
        handoff::Package {
            identity: format!("nxrs-{capability} v0.0.0 ({})", directory.display()),
            directory,
            features: BTreeSet::new(),
        }
    });
    let receipt = handoff::prepare(
        &fixture.sdk(),
        &profile,
        target,
        "nuttx",
        &profile_receipt,
        &packages,
        &fixture.command(),
        &fixture.out().join("physical"),
    )
    .unwrap();
    let imu = output_named(&receipt, "imu_config.rs");
    let gnss = output_named(&receipt, "gnss_config.rs");
    let witness = fixture.root().join("physical_values.rs");
    fs::write(&witness, format!(r#"
mod motion {{
    struct Binding<'a> {{ path: &'a str, address: u8, frequency: u32, irq_path: Option<&'a str> }}
    include!({imu:?});
    pub fn describe() -> String {{
        let model = match MODEL {{ SensorModel::Bmi270 => "bmi270", SensorModel::Lsm6dso => "lsm6dso" }};
        format!("{{model}} {{}} {{}} {{}} {{:?}} {{STACK_BYTES}}", BINDING.path, BINDING.address, BINDING.frequency, BINDING.irq_path)
    }}
}}
mod position {{
    include!({gnss:?});
    pub fn describe() -> String {{ format!("{{DEVICE_PATH}} {{BAUD}} {{STACK_BYTES}}") }}
}}
fn main() {{ println!("{{}} | {{}}", motion::describe(), position::describe()); }}
"#)).unwrap();
    let binary = fixture
        .root()
        .join(format!("physical_values{}", std::env::consts::EXE_SUFFIX));
    success(
        &Command::new("rustc")
            .arg(witness)
            .arg("-o")
            .arg(&binary)
            .output()
            .unwrap(),
    );
    let output = Command::new(binary).output().unwrap();
    success(&output);
    assert_eq!(
        String::from_utf8(output.stdout).unwrap().trim(),
        "bmi270 /dev/i2c0 104 400000 None 16384 | /dev/ttyS1 38400 16384"
    );
}

#[cfg(unix)]
#[test]
fn native_preparation_compiles_only_consumed_enabled_helpers_and_cargo_only_links() {
    use std::os::unix::fs::PermissionsExt;
    if !["linux", "macos"].contains(&std::env::consts::OS) {
        return;
    }
    let fixture = Fixture::new("wifi");
    let os = std::env::consts::OS;
    fixture.write(
        "sdk/Cargo.toml",
        "[workspace]\nmembers=[\"hal/wifi\",\"hal/ble\",\"service\"]\nresolver=\"2\"\n",
    );
    for capability in ["wifi", "ble"] {
        fixture.write(
            &format!("sdk/hal/{capability}/Cargo.toml"),
            &format!(
                "[package]\nname=\"nxrs-{capability}\"\nversion=\"0.0.0\"\nedition=\"2021\"\n\
             [package.metadata.nxrs]\nkind=\"hal-capability\"\nplatforms=[{os:?}]\n\
             [package.metadata.nxrs.platform.{os}]\nrequired-features=[\"native\"]\n\
             [features]\ndefault=[]\nnative=[]\nprobe=[]\n\
             [build-dependencies]\nnxrs-build-support={{path={:?}}}\n",
                support()
            ),
        );
        fixture.write(
            &format!("sdk/hal/{capability}/src/lib.rs"),
            r#"
#[cfg(all(hal_provider="hardware", feature="native"))]
mod selected {
    extern "C" { fn prepared_native_value() -> u64; }
    pub fn value() -> u64 { unsafe { prepared_native_value() } }
}
#[cfg(not(all(hal_provider="hardware", feature="native")))]
mod selected { pub fn value() -> u64 { 0 } }
pub use selected::value;
"#,
        );
        fixture.write(&format!("sdk/hal/{capability}/src/platform/{os}/ffi/value.c"),
            if capability == "wifi" {
                "#include <stdint.h>\n#include \"abi.h\"\nuint64_t prepared_native_value(void) { return NATIVE_VALUE; }\n"
            } else { "#error unconsumed helper must never compile\n" });
    }
    fixture.write(
        &format!("sdk/hal/wifi/src/platform/{os}/ffi/abi.h"),
        "#define NATIVE_VALUE 17\n",
    );
    fixture.write(
        "sdk/platform/profiles/host-helper.toml",
        &format!("target-os=[{os:?}]\n[hal]\nwifi={os:?}\nble={os:?}\n"),
    );
    let tools = fixture.root().join("instrumented tools");
    fs::create_dir_all(&tools).unwrap();
    let program = if os == "macos" { "xcrun" } else { "cc" };
    let compiler = nxrs_build::find_program(program).unwrap();
    let log = fixture.root().join("native-compilations");
    let wrapper = tools.join(program);
    fs::write(&wrapper, format!(
        "#!/bin/sh\nfor arg do\n  if [ \"$arg\" = -c ]; then printf 'compile\\n' >> {}; fi\ndone\nexec {} \"$@\"\n",
        nxrs_build::shell_quote(log.to_str().unwrap()),
        nxrs_build::shell_quote(compiler.to_str().unwrap()),
    )).unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();
    let path = std::env::join_paths(
        std::iter::once(tools).chain(std::env::split_paths(&std::env::var_os("PATH").unwrap())),
    )
    .unwrap();
    let base = || {
        let mut command = fixture.command();
        command.env("PATH", &path).env("CC", "cc");
        command
    };
    let mut command = base();
    command.args(["--features", "helper"]);
    let receipt = fixture.prepare("host-helper", &mut command);
    let archive = output_named(&receipt, "libwifi_native.a");
    assert!(archive.is_file());
    let compiled = fs::read(&log).unwrap();
    assert_eq!(String::from_utf8_lossy(&compiled).lines().count(), 1);
    let prepared_stamps = stamps([receipt.clone(), archive.clone()]);
    for mode in ["build", "check", "test", "doc"] {
        success(&action(&command, mode).output().unwrap());
        assert_eq!(
            fs::read(&log).unwrap(),
            compiled,
            "{mode} compiled an already prepared helper"
        );
    }
    assert_eq!(fixture.value(), "17");
    let mut same = base();
    same.args(["--features", "helper"]);
    assert_eq!(fixture.prepare("host-helper", &mut same), receipt);
    assert_eq!(stamps(prepared_stamps.keys().cloned()), prepared_stamps);
    assert_eq!(fs::read(&log).unwrap(), compiled);

    if os == "linux" {
        // A Cargo-only forced environment setting must not silently select a
        // compiler different from the controller's explicit native context.
        let mut cargo_only = action(&command, "build");
        cargo_only.args([
            "--config",
            "env.CC.value='unprepared-compiler'",
            "--config",
            "env.CC.force=true",
        ]);
        failure(&cargo_only.output().unwrap(), "prepared native environment");
        assert_eq!(fs::read(&log).unwrap(), compiled);
    }

    let mut wrapper_source = fs::read_to_string(&wrapper).unwrap();
    wrapper_source.push_str("\n# same-path compiler revision\n");
    fs::write(&wrapper, wrapper_source).unwrap();
    failure(
        &action(&command, "build").output().unwrap(),
        "stale prepared inputs",
    );
    assert_eq!(fs::read(&log).unwrap(), compiled);
    command = base();
    command.args(["--features", "helper"]);
    fixture.prepare("host-helper", &mut command);
    success(&command.output().unwrap());
    assert_eq!(fixture.value(), "17");
    let compiled = fs::read(&log).unwrap();
    assert_eq!(String::from_utf8_lossy(&compiled).lines().count(), 2);

    let header = fixture
        .hal()
        .join("src/platform")
        .join(os)
        .join("ffi/abi.h");
    fs::write(header, "#define NATIVE_VALUE 23\n").unwrap();
    failure(
        &action(&command, "build").output().unwrap(),
        "stale prepared inputs",
    );
    assert_eq!(fs::read(&log).unwrap(), compiled);
    let mut refreshed = base();
    refreshed.args(["--features", "helper"]);
    fixture.prepare("host-helper", &mut refreshed);
    success(&refreshed.output().unwrap());
    assert_eq!(fixture.value(), "23");
    let compiled = fs::read(&log).unwrap();
    assert_eq!(String::from_utf8_lossy(&compiled).lines().count(), 3);

    // A consumed HAL without its required helper feature must not compile C,
    // even when its profile still admits the hardware provider.
    fs::write(
        fixture
            .hal()
            .join("src/platform")
            .join(os)
            .join("ffi/value.c"),
        "#error feature-disabled helper must never compile\n",
    )
    .unwrap();
    let mut disabled = base();
    let disabled_receipt = fixture.prepare("host-helper", &mut disabled);
    assert!(!outputs(&disabled_receipt)
        .iter()
        .any(|path| path.extension().is_some_and(|extension| extension == "a")));
    success(&disabled.output().unwrap());
    assert_eq!(fixture.value(), "0");
    assert_eq!(fs::read(&log).unwrap(), compiled);
}
