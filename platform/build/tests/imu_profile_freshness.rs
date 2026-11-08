//! Compile both production declarative sensor schemas in one detached Cargo output.
use nxrs_build::{profile::Profile, TempDir};
use nxrs_build_support::resolved;
use std::{fs, path::Path, process::Command};

#[test]
fn profile_switch_refreshes_generated_sensor_bindings_in_the_same_cargo_output() {
    let source = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let temporary = TempDir::new(&std::env::temp_dir()).unwrap();
    let root = &temporary.0;
    let sdk = root.join("sdk");
    fs::create_dir_all(sdk.join("platform/profiles")).unwrap();
    fs::create_dir_all(sdk.join("platform/nuttx/platforms")).unwrap();
    fs::write(sdk.join("Cargo.toml"), "[workspace]\n").unwrap();
    for capability in ["imu", "gnss"] {
        let package = sdk.join("hal").join(capability);
        fs::create_dir_all(package.join("bindings")).unwrap();
        fs::copy(
            source.join("hal").join(capability).join("Cargo.toml"),
            package.join("Cargo.toml"),
        )
        .unwrap();
        for file in fs::read_dir(source.join("hal").join(capability).join("bindings")).unwrap() {
            let file = file.unwrap();
            fs::copy(file.path(), package.join("bindings").join(file.file_name())).unwrap();
        }
    }
    fs::copy(
        source.join("platform/nuttx/platforms/m5stack-cores3-gnss.toml"),
        sdk.join("platform/nuttx/platforms/m5stack-cores3-gnss.toml"),
    )
    .unwrap();
    fs::create_dir(root.join("src")).unwrap();
    fs::write(
        root.join("Cargo.toml"),
        format!(
            "[package]\nname = \"sensor-binding-witness\"\nversion = \"0.0.0\"\nedition = \"2021\"\n\
             [build-dependencies]\nnxrs-build-support = {{ path = {:?} }}\n[workspace]\n",
            source.join("platform/build-support")
        ),
    )
    .unwrap();
    fs::write(
        root.join("build.rs"),
        r#"
use nxrs_build_support::{preparation, resolved};
use std::{env, path::PathBuf};
fn main() {
    let root = PathBuf::from(env::var_os("NXRS_SDK_ROOT").unwrap());
    let receipt = PathBuf::from(env::var_os("NXRS_RESOLVED_PROFILE").unwrap());
    println!("cargo:rerun-if-changed={}", receipt.display());
    // Render the actual NuttX schemas on the host without compiling native helpers.
    let profile = resolved::load(&receipt, &root, Some("m5stack-cores3-gnss"), "xtensa-esp32s3-nuttx", "nuttx").unwrap();
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    for capability in ["imu", "gnss"] {
        let package_output = output.join(capability);
        std::fs::create_dir_all(&package_output).unwrap();
        for path in preparation::prepare_package(&root.join("hal").join(capability), &profile, "nuttx", &package_output).unwrap() {
            println!("cargo:rerun-if-changed={}", path.display());
        }
    }
}
"#,
    )
    .unwrap();
    fs::write(
        root.join("src/main.rs"),
        r#"
mod motion {
    struct Binding<'a> {
        path: &'a str,
        address: u8,
        frequency: u32,
        irq_path: Option<&'a str>,
    }
    include!(concat!(env!("OUT_DIR"), "/imu/imu_config.rs"));
    pub fn describe() -> String {
        let model = match MODEL { SensorModel::Bmi270 => "bmi270", SensorModel::Lsm6dso => "lsm6dso" };
        format!("{model} {} {} {} {:?} {STACK_BYTES}",
            BINDING.path, BINDING.address, BINDING.frequency, BINDING.irq_path)
    }
}
mod position {
    include!(concat!(env!("OUT_DIR"), "/gnss/gnss_config.rs"));
    pub fn describe() -> String {
        format!("{DEVICE_PATH} {BAUD} {STACK_BYTES}")
    }
}
fn main() {
    println!("{} | {}", motion::describe(), position::describe());
}
"#,
    )
    .unwrap();
    let cargo = env!("CARGO");
    let version = nxrs_build::output(Command::new(cargo).current_dir(root).arg("-vV")).unwrap();
    let target = version
        .lines()
        .find_map(|line| line.strip_prefix("host: "))
        .unwrap();
    let patch = root.join("alternate.toml");
    fs::write(
        &patch,
        "[resources.sensor_bus]\ndevice = \"/dev/i2c1\"\nfrequency = 100000\n\
         [devices.motion]\naddress = 105\n\
         [resources.receiver_uart]\ndevice = \"/dev/ttyS2\"\nbaud = 115200\n\
         [gnss]\nstack-bytes = 32768\n",
    )
    .unwrap();
    let replacement = source.join("tests/host/fixtures/lsm6dso-overlay.toml");
    let receipt = root.join("resolved-profile.json");
    let run = || {
        Command::new(cargo)
            .current_dir(root)
            .args(["run", "--offline", "--quiet", "--target", target])
            .env("CARGO_TARGET_DIR", root.join("target"))
            .env("NXRS_SDK_ROOT", &sdk)
            .env("NXRS_RESOLVED_PROFILE", &receipt)
            .output()
            .unwrap()
    };
    let original = "bmi270 /dev/i2c0 104 400000 None 16384 | /dev/ttyS1 38400 16384";
    for (overlays, expected) in [
        (&[][..], original),
        (
            &[patch][..],
            "bmi270 /dev/i2c1 105 100000 None 16384 | /dev/ttyS2 115200 32768",
        ),
        (
            &[replacement][..],
            "lsm6dso /dev/i2c1 106 100000 None 16384 | /dev/ttyS1 38400 16384",
        ),
        (&[][..], original),
    ] {
        let profile =
            Profile::resolve_with_overlays(&sdk, "m5stack-cores3-gnss", overlays).unwrap();
        resolved::write(&receipt, &sdk, &profile, "xtensa-esp32s3-nuttx", "nuttx").unwrap();
        let result = run();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        assert_eq!(String::from_utf8_lossy(&result.stdout).trim(), expected);
    }

    // Content changes at the same declared path must invalidate both Cargo and
    // the receipt, even when the resolved values are semantically unchanged.
    for relative in ["hal/imu/bindings/i2c.toml", "hal/gnss/bindings/nuttx.toml"] {
        let path = sdk.join(relative);
        let mut content = fs::read_to_string(&path).unwrap();
        content.push_str("\n# same-path freshness witness\n");
        fs::write(&path, content).unwrap();
        let stale = run();
        assert!(!stale.status.success(), "stale {relative} was accepted");
        assert!(
            String::from_utf8_lossy(&stale.stderr).contains("stale"),
            "{}",
            String::from_utf8_lossy(&stale.stderr)
        );
        let profile = Profile::resolve(&sdk, "m5stack-cores3-gnss").unwrap();
        resolved::write(&receipt, &sdk, &profile, "xtensa-esp32s3-nuttx", "nuttx").unwrap();
        let refreshed = run();
        assert!(
            refreshed.status.success(),
            "{}",
            String::from_utf8_lossy(&refreshed.stderr)
        );
        assert_eq!(String::from_utf8_lossy(&refreshed.stdout).trim(), original);
    }
}
