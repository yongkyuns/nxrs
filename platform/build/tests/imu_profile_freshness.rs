//! Compile the real IMU emitter's output in one detached host Cargo target directory.
use nxrs_build::{profile::Profile, TempDir};
use nxrs_build_support::resolved;
use std::{fs, path::Path, process::Command};

#[test]
fn profile_switch_refreshes_generated_imu_binding_in_the_same_cargo_output() {
    let sdk = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let temporary = TempDir::new(&std::env::temp_dir()).unwrap();
    let root = &temporary.0;
    fs::create_dir(root.join("src")).unwrap();
    fs::write(
        root.join("Cargo.toml"),
        format!(
            "[package]\nname = \"imu-binding-witness\"\nversion = \"0.0.0\"\nedition = \"2021\"\n\
             [build-dependencies]\nnxrs-build-support = {{ path = {:?} }}\n[workspace]\n",
            sdk.join("platform/build-support")
        ),
    )
    .unwrap();
    fs::write(
        root.join("build.rs"),
        format!(
            r#"
#[path = {emitter:?}]
mod emit;
use nxrs_build_support::resolved;
use std::{{env, path::PathBuf}};
fn main() {{
    let root = PathBuf::from(env::var_os("NXRS_SDK_ROOT").unwrap());
    let receipt = PathBuf::from(env::var_os("NXRS_RESOLVED_PROFILE").unwrap());
    println!("cargo:rerun-if-changed={{}}", receipt.display());
    // Exercise NuttX binding generation on the host without loading the native SDK.
    let profile = resolved::load(&receipt, &root, Some("m5stack-cores3-gnss"), "xtensa-esp32s3-nuttx", "nuttx").unwrap();
    let graph = profile.composition().unwrap().unwrap();
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("imu_config.rs");
    emit::write(&output, graph.imu().unwrap()).unwrap();
}}
"#,
            emitter = sdk.join("hal/imu/build/emit.rs")
        ),
    )
    .unwrap();
    fs::write(
        root.join("src/main.rs"),
        r#"
struct Binding<'a> {
    path: &'a str,
    address: u8,
    frequency: u32,
    irq_path: Option<&'a str>,
}
include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));
fn main() {
    let model = match MODEL { SensorModel::Bmi270 => "bmi270", SensorModel::Lsm6dso => "lsm6dso" };
    println!("{model} {} {} {} {:?} {STACK_BYTES}",
        BINDING.path, BINDING.address, BINDING.frequency, BINDING.irq_path);
}
"#,
    )
    .unwrap();
    let cargo = env!("CARGO");
    let target = nxrs_build::output(Command::new(cargo).current_dir(root).arg("-vV")).unwrap();
    let target = target
        .lines()
        .find_map(|line| line.strip_prefix("host: "))
        .unwrap();
    let patch = root.join("alternate.toml");
    fs::write(
        &patch,
        "[resources.legacy_i2c]\ndevice = \"/dev/i2c1\"\nfrequency = 100000\n\
         [devices.imu]\naddress = 105\n",
    )
    .unwrap();
    let replacement = sdk.join("tests/host/fixtures/lsm6dso-overlay.toml");
    let receipt = root.join("resolved-profile.json");
    for (overlays, expected) in [
        (&[][..], "bmi270 /dev/i2c0 104 400000 None 16384"),
        (&[patch][..], "bmi270 /dev/i2c1 105 100000 None 16384"),
        (
            &[replacement][..],
            "lsm6dso /dev/i2c1 106 100000 None 16384",
        ),
        (&[][..], "bmi270 /dev/i2c0 104 400000 None 16384"),
    ] {
        let profile =
            Profile::resolve_with_overlays(&sdk, "m5stack-cores3-gnss", overlays).unwrap();
        resolved::write(&receipt, &sdk, &profile, "xtensa-esp32s3-nuttx", "nuttx").unwrap();
        let result = Command::new(cargo)
            .current_dir(root)
            .args(["run", "--offline", "--quiet", "--target", target])
            .env("CARGO_TARGET_DIR", root.join("target"))
            .env("NXRS_SDK_ROOT", &sdk)
            .env("NXRS_RESOLVED_PROFILE", &receipt)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        assert_eq!(String::from_utf8_lossy(&result.stdout).trim(), expected);
    }
}
