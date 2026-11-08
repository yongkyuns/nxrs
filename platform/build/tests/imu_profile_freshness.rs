//! Compile both production sensor resource bindings in one detached Cargo output.
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
        fs::create_dir_all(&package).unwrap();
        for file in ["Cargo.toml", "bindings.toml"] {
            fs::copy(
                source.join("hal").join(capability).join(file),
                package.join(file),
            )
            .unwrap();
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
             [features]\nprobe = []\n[dependencies]\nnxrs-messaging = {{ path = {:?} }}\n\
             nxrs-acquisition = {{ path = {:?} }}\nnxrs-i2c = {{ path = {:?} }}\n\
             nxrs-bmi270 = {{ path = {:?} }}\nnxrs-lsm6dso = {{ path = {:?} }}\nembedded-hal = '=1.0.0'\n\
             [build-dependencies]\nnxrs-build-support = {{ path = {:?} }}\n[workspace]\n",
            source.join("shared/messaging"), source.join("driver/acquisition"), source.join("driver/i2c"),
            source.join("driver/bmi270"), source.join("driver/lsm6dso"), source.join("platform/build-support")
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
    // Render the actual NuttX resource bindings on the host without compiling native helpers.
    let profile = resolved::load(&receipt, &root, Some("m5stack-cores3-gnss"), "xtensa-esp32s3-nuttx", "nuttx").unwrap();
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    for capability in ["imu", "gnss"] {
        let package_output = output.join(capability);
        std::fs::create_dir_all(&package_output).unwrap();
        for path in preparation::prepare_package(&root.join("hal").join(capability), &profile, "nuttx", &package_output).unwrap() {
            println!("cargo:rerun-if-changed={}", path.display());
        }
    }
    std::fs::copy(output.join("imu/imu_config.rs"), output.join("imu_config.rs")).unwrap();
}
"#,
    )
    .unwrap();
    fs::write(
        root.join("src/main.rs"),
        r#"
#![allow(dead_code, unused_imports)]
#[path = @API@] mod api;
pub use api::{AppliedConfig, Config, DeviceError, Event, Imu, ImuSample};
// Every host checks generated bindings below. Unix hosts additionally compile
// the actual NuttX factory, whose descriptor and cancellation APIs require Unix.
#[cfg(unix)]
#[path = @PROBE@] pub mod probe;
#[cfg(unix)]
mod ffi {
    pub fn transfer(_: i32, _: u8, _: u32, _: &[u8], _: &mut [u8]) -> std::io::Result<()> {
        panic!("resolving sensor settings must not access the bus")
    }
}
#[cfg(unix)]
#[path = @RESOURCE@] mod resource;
#[cfg(unix)]
#[path = @FACTORY@] mod production;
#[cfg(unix)]
mod platform { pub(crate) use crate::production::open_probed; }
mod motion {
    struct Binding<'a> {
        path: &'a str,
        address: u8,
        frequency: u32,
        irq_path: Option<&'a str>,
    }
    mod implementations {
        pub struct Bmi270;
        impl Bmi270 { pub const NAME: &str = "bmi270"; pub const RATE: u32 = 100000; }
        pub struct Lsm6dso;
        impl Lsm6dso { pub const NAME: &str = "lsm6dso"; pub const RATE: u32 = 104000; }
    }
    include!(concat!(env!("OUT_DIR"), "/imu/imu_config.rs"));
    pub fn expected_rate() -> u32 { Implementation::RATE }
    pub fn describe() -> String {
        let model = Implementation::NAME;
        format!("{model} {} {} {} {:?} {STACK_BYTES}",
            BINDING.path, BINDING.address, BINDING.frequency, BINDING.irq_path)
    }
}
mod position {
    struct Binding<'a> { path: &'a str, baud: u32 }
    mod implementations { pub struct NeoM9n; }
    include!(concat!(env!("OUT_DIR"), "/gnss/gnss_config.rs"));
    pub fn describe() -> String {
        format!("{} {} {STACK_BYTES}", BINDING.path, BINDING.baud)
    }
}
fn main() {
    #[cfg(unix)] {
        let applied = production::resolve(Config {
            min_rate_millihz: 100000, max_rate_millihz: 110000,
            accel_range_g: 4, gyro_range_dps: 2000, max_delivery_interval_us: 10000,
        }).unwrap();
        assert_eq!(applied.rate_millihz, motion::expected_rate());
    }
    println!("{} | {}", motion::describe(), position::describe());
}
"#
        .replace("@API@", &format!("{:?}", source.join("hal/imu/src/api.rs")))
        .replace(
            "@PROBE@",
            &format!("{:?}", source.join("hal/imu/src/probe.rs")),
        )
        .replace(
            "@RESOURCE@",
            &format!("{:?}", source.join("driver/i2c/tests/support/mod.rs")),
        )
        .replace(
            "@FACTORY@",
            &format!("{:?}", source.join("hal/imu/src/platform/nuttx/mod.rs")),
        ),
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
         [resources.receiver_uart]\ndevice = \"/dev/ttyS2\"\nbaud = 115200\n",
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
            "bmi270 /dev/i2c1 105 100000 None 16384 | /dev/ttyS2 115200 16384",
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
    for relative in ["hal/imu/bindings.toml", "hal/gnss/bindings.toml"] {
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
