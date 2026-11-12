//! Independent product, composition and driver packages through the public frontend.
//!
//! The host witness compiles the real generated declarations against a small
//! typed construction contract. It performs no device I/O and does not qualify
//! a NuttX ABI or hardware. Production IMU factory coverage remains in
//! imu_profile_freshness.rs.
use nxrs_build::{cargo_graph, profile::Profile, TempDir};
use nxrs_build_support::{preparation, resolved};
use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

fn write(root: &Path, relative: &str, text: &str) {
    let path = root.join(relative);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn success(output: Output) -> String {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

fn frontend(sdk: &Path, product: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_nxrs-build"));
    command
        .current_dir(product)
        .arg("--workspace-root")
        .arg(sdk);
    command
}

fn fixture(product: &Path, sdk: &Path) {
    write(
        product,
        "Cargo.toml",
        r#"
[workspace]
members = ["app", "wiring"]
resolver = "2"
[workspace.metadata.nxrs]
device-catalogue = "wiring"
[workspace.dependencies]
devices = { package = "external-wiring", path = "wiring", features = ["imu"] }
chip = { package = "external-chip-a", path = "sensor-a" }
"#,
    );
    write(
        product,
        "app/Cargo.toml",
        r#"
[package]
name = "external-product"
version = "0.1.0"
edition = "2021"
[dependencies]
devices = { workspace = true }
"#,
    );
    write(
        product,
        "app/src/main.rs",
        r#"
use devices::Device;
fn main() {
    let primary = devices::primary_imu();
    assert_eq!(primary as *const _ as *const (), devices::backup() as *const _ as *const ());
    assert_eq!(primary.id(), "imu");
    assert_eq!(primary.address(), 104);
    println!("{}", primary.model());
}
"#,
    );
    write(
        product,
        "wiring/Cargo.toml",
        &format!(
            r#"
[package]
name = "external-wiring"
version = "0.1.0"
edition = "2021"
[package.metadata.nxrs]
device-capabilities = ["imu"]
[features]
imu = []
probe = []
# The ordinary dependency makes this driver's typed factory testable on the
# host; the physical declaration registers the same optional Cargo edge.
[dependencies]
chip = {{ workspace = true, optional = true }}
[target.'cfg(target_os = "nuttx")'.dependencies]
chip = {{ workspace = true, optional = true }}
[build-dependencies]
nxrs-build-support = {{ path = {:?} }}
"#,
            sdk.join("tools/build-support")
        ),
    );
    write(
        product,
        "wiring/build.rs",
        r#"
use nxrs_build_support::{preparation, resolved};
use std::{env, path::PathBuf};
fn main() {
    // Exercise the normal frontend receipt and prepared-input adapter first.
    nxrs_build_support::configure_composition().unwrap();
    // Independently prepared physical configuration is compiled with host-only
    // resource types. These fixture inputs never select a production provider.
    let root = PathBuf::from(env::var_os("NXRS_SDK_ROOT").unwrap());
    let receipt = PathBuf::from(env::var_os("FACTORY_PROFILE_RECEIPT").unwrap());
    let handoff = PathBuf::from(env::var_os("FACTORY_CONFIGURATION").unwrap());
    println!("cargo:rerun-if-env-changed=FACTORY_PROFILE_RECEIPT");
    println!("cargo:rerun-if-env-changed=FACTORY_CONFIGURATION");
    println!("cargo:rerun-if-changed={}", receipt.display());
    let profile = resolved::load(&receipt, &root, None, "xtensa-esp32s3-nuttx", "nuttx").unwrap();
    let directory = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    for path in preparation::consume(&handoff, &directory, "imu", &profile,
        "xtensa-esp32s3-nuttx", "nuttx", &out).unwrap().expect("physical factory input") {
        println!("cargo:rerun-if-changed={}", path.display());
    }
}
"#,
    );
    write(
        product,
        "wiring/src/lib.rs",
        r#"
pub trait Device {
    fn id(&self) -> &'static str;
    fn model(&self) -> &'static str;
    fn address(&self) -> u8;
}
mod resource {
    pub struct Config { pub path: &'static str, pub frequency: u32 }
}
mod resources {
    pub struct Bus;
    pub struct I2cResource;
    pub fn open_i2c(_: (super::resource::Config, u8), _: ()) -> I2cResource { panic!("no hardware I/O") }
}
struct Instance<S> {
    id: &'static str,
    model: &'static str,
    address: u8,
    sensor: std::marker::PhantomData<fn() -> S>,
}
impl<S> Device for Instance<S> {
    fn id(&self) -> &'static str { self.id }
    fn model(&self) -> &'static str { self.model }
    fn address(&self) -> u8 { self.address }
}
macro_rules! declare_device {
    ($name:ident, $id:literal, $sensor:ty, $resource:ty, $open:path, $binding:expr, $irq:expr, $stack:expr $(, $reference:tt)?) => {
        const _: fn() = || {
            let _: $resource = $open($binding, ());
            let _: Option<&str> = $irq;
            let _: usize = $stack;
            let binding = $binding;
            let _: &str = binding.0.path;
            let _: u32 = binding.0.frequency;
        };
        static $name: Instance<$sensor> = Instance {
            id: $id, model: <$sensor>::MODEL, address: ($binding).1,
            sensor: std::marker::PhantomData,
        };
    };
}
include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));
pub use devices::{backup, primary_imu};
"#,
    );
    for suffix in ["a", "b"] {
        write(
            product,
            &format!("sensor-{suffix}/Cargo.toml"),
            &format!(
                r#"
[package]
name = "external-chip-{suffix}"
version = "0.1.0"
edition = "2021"
[package.metadata.nxrs]
bindings = ["bindings.toml"]
[package.metadata.nxrs.device]
capability = "imu"
"#
            ),
        );
        write(
            product,
            &format!("sensor-{suffix}/src/lib.rs"),
            r#"
pub struct ExternalSensor<B>(std::marker::PhantomData<B>);
impl<B> ExternalSensor<B> { pub const MODEL: &'static str = env!("CARGO_PKG_NAME"); }
"#,
        );
        write(
            product,
            &format!("sensor-{suffix}/bindings.toml"),
            r#"
version = 2
[[binding]]
compatible = "product,external-sensor"
bus = "i2c"
implementation = "ExternalSensor"
addresses = [104]
frequencies = [100000, 400000]
interrupt = false
"#,
        );
    }
    write(
        product,
        "sensor.toml",
        r#"
[devices.imu]
compatible = "product,external-sensor"
bus = "sensor_bus"
address = 104
wake = { kind = "periodic" }
[aliases]
imu_backup = "imu"
"#,
    );
}

fn lock(product: &Path) {
    success(
        Command::new(env!("CARGO"))
            .current_dir(product)
            .args(["generate-lockfile", "--offline"])
            .output()
            .unwrap(),
    );
}

fn physical_profile(sdk: &Path, product: &Path) -> Profile {
    Profile::resolve_for_manifest(
        sdk,
        "m5stack-cores3-gnss",
        &[product.join("sensor.toml")],
        Some(&product.join("app/Cargo.toml")),
    )
    .unwrap()
}

fn prepare(sdk: &Path, product: &Path, profile: &Profile) -> (PathBuf, PathBuf, Vec<String>) {
    let mut build = Command::new(env!("CARGO"));
    build
        .current_dir(product)
        .args(["build", "--offline", "--locked", "--manifest-path"])
        .arg(product.join("app/Cargo.toml"));
    let selected = cargo_graph::resolve(&mut build, profile).unwrap();
    assert_eq!(selected.driver_features, ["devices/chip"]);
    assert!(!selected
        .packages
        .iter()
        .any(|name| name.starts_with("nxrs-bmi270 v") || name.starts_with("nxrs-ubx-driver v")));
    let receipt = product.join("physical-profile.json");
    resolved::write(&receipt, sdk, profile, "xtensa-esp32s3-nuttx", "nuttx").unwrap();
    let handoff = preparation::prepare_consumers(
        selected
            .path_packages()
            .map(|(_, path, features)| (path, features)),
        profile,
        "xtensa-esp32s3-nuttx",
        "nuttx",
        &product.join("physical-configuration"),
    )
    .unwrap()
    .unwrap();
    (receipt, handoff, selected.driver_features)
}

fn build_and_run(
    sdk: &Path,
    product: &Path,
    receipt: &Path,
    handoff: &Path,
    features: &[String],
) -> String {
    success(
        frontend(sdk, product)
            .args(["build", "--manifest-path"])
            .arg(product.join("app/Cargo.toml"))
            .args([
                "--profile",
                "mock",
                "--features",
                &features.join(","),
                "--out",
            ])
            .arg(product.join("artifacts"))
            .env("FACTORY_PROFILE_RECEIPT", receipt)
            .env("FACTORY_CONFIGURATION", handoff)
            .env("CARGO_NET_OFFLINE", "true")
            .output()
            .unwrap(),
    );
    success(
        Command::new(
            product
                .join("artifacts/cargo/release")
                .join(format!("external-product{}", std::env::consts::EXE_SUFFIX)),
        )
        .output()
        .unwrap(),
    )
}

#[test]
fn product_owned_packages_compile_and_inherited_reselection_invalidates_receipts() {
    let sdk = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let temporary = TempDir::new(&std::env::temp_dir()).unwrap();
    let product = temporary.0.join("product");
    fs::create_dir(&product).unwrap();
    // Exercise the alternate spelling used by macOS temporary directories on
    // every Unix host, while preserving the full product build/freshness witness.
    #[cfg(unix)]
    let product = {
        let alias = temporary.0.join("product-link");
        std::os::unix::fs::symlink(product.canonicalize().unwrap(), &alias).unwrap();
        alias
    };
    let product = &product;
    fixture(product, &sdk);
    lock(product);
    let original_app = fs::read(product.join("app/src/main.rs")).unwrap();
    let original_catalogue = fs::read(product.join("wiring/Cargo.toml")).unwrap();
    let inspected = success(
        frontend(&sdk, product)
            .args([
                "profile",
                "--platform",
                "m5stack-cores3-gnss",
                "--manifest-path",
            ])
            .arg(product.join("app/Cargo.toml"))
            .arg("--overlay")
            .arg(product.join("sensor.toml"))
            .output()
            .unwrap(),
    );
    assert!(inspected.contains("external-wiring"));
    assert!(inspected.contains("product,external-sensor"));
    success(
        frontend(&sdk, product)
            .args(["build", "--manifest-path"])
            .arg(product.join("app/Cargo.toml"))
            .args(["--profile", "m5stack-cores3-gnss", "--overlay"])
            .arg(product.join("sensor.toml"))
            .arg("--dry-run")
            .output()
            .unwrap(),
    );

    let physical = physical_profile(&sdk, product);
    let (receipt, handoff, features) = prepare(&sdk, product, &physical);
    let saved: Value = serde_json::from_slice(&fs::read(&receipt).unwrap()).unwrap();
    for relative in [
        "Cargo.toml",
        "app/Cargo.toml",
        "wiring/Cargo.toml",
        "sensor-a/Cargo.toml",
        "sensor-a/bindings.toml",
    ] {
        let expected = product.join(relative).canonicalize().unwrap();
        assert!(
            saved["dependencies"]
                .as_array()
                .unwrap()
                .iter()
                .any(|input| input["path"].as_str().map(Path::new) == Some(expected.as_path())),
            "missing {relative}"
        );
    }
    assert_eq!(
        build_and_run(&sdk, product, &receipt, &handoff, &features).trim(),
        "external-chip-a"
    );

    // Swap the independent driver through one ordinary inherited Cargo edge.
    // Neither application/composition source nor SDK registration code changes.
    let manifest = product.join("Cargo.toml");
    let text = fs::read_to_string(&manifest).unwrap();
    fs::write(
        &manifest,
        text.replace("external-chip-a", "external-chip-b")
            .replace("sensor-a", "sensor-b"),
    )
    .unwrap();
    let stale = resolved::load(&receipt, &sdk, None, "xtensa-esp32s3-nuttx", "nuttx").unwrap_err();
    assert!(stale.contains("stale"), "{stale}");
    let changed = physical_profile(&sdk, product);
    let out = product.join("stale-output");
    fs::create_dir(&out).unwrap();
    assert!(preparation::consume(
        &handoff,
        &product.join("wiring"),
        "imu",
        &changed,
        "xtensa-esp32s3-nuttx",
        "nuttx",
        &out
    )
    .unwrap_err()
    .contains("stale"));
    lock(product);
    let (receipt, replacement, features) = prepare(&sdk, product, &changed);
    assert_ne!(handoff, replacement);
    assert_eq!(
        build_and_run(&sdk, product, &receipt, &replacement, &features).trim(),
        "external-chip-b"
    );
    assert_eq!(
        original_app,
        fs::read(product.join("app/src/main.rs")).unwrap()
    );
    assert_eq!(
        original_catalogue,
        fs::read(product.join("wiring/Cargo.toml")).unwrap()
    );

    // Receipt validation observes the selector itself, including a changed
    // product workspace, rather than replaying a previously trusted path.
    let text = fs::read_to_string(&manifest).unwrap();
    fs::write(
        &manifest,
        text.replace(
            "device-catalogue = \"wiring\"",
            "device-catalogue = \"missing\"",
        ),
    )
    .unwrap();
    assert!(resolved::load(&receipt, &sdk, None, "xtensa-esp32s3-nuttx", "nuttx").is_err());
}
