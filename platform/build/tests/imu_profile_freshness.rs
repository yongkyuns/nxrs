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
    for (relative, files) in [
        ("hal/imu", &["Cargo.toml"][..]),
        ("hal/gnss", &["Cargo.toml", "bindings.toml"][..]),
        ("driver/bmi270", &["Cargo.toml", "bindings.toml"][..]),
        ("driver/lsm6dso", &["Cargo.toml", "bindings.toml"][..]),
    ] {
        let package = sdk.join(relative);
        fs::create_dir_all(&package).unwrap();
        for file in files {
            fs::copy(source.join(relative).join(file), package.join(file)).unwrap();
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
             nxrs-acquisition = {{ path = {:?} }}\nnxrs-i2c = {{ path = {:?} }}\nnxrs-gpio = {{ path = {:?} }}\nnxrs-spi = {{ path = {:?} }}\n\
             nxrs-peripheral = {{ path = {:?} }}\n\
             nxrs-bmi270 = {{ path = {:?} }}\nnxrs-lsm6dso = {{ path = {:?} }}\nembedded-hal = '=1.0.0'\n\
             [build-dependencies]\nnxrs-build-support = {{ path = {:?} }}\n[workspace]\n",
            source.join("shared/messaging"), source.join("hal/acquisition"), source.join("driver/i2c"), source.join("driver/gpio"), source.join("driver/spi"),
            source.join("driver/peripheral"), source.join("driver/bmi270"), source.join("driver/lsm6dso"), source.join("platform/build-support")
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
pub use api::{AppliedConfig, Config, DeviceError, Event, ImuSample, Session};
#[cfg(unix)] pub use api::Device;
#[path = @INSTANCE@] mod instance;
#[cfg(not(unix))] pub trait Device {}
// Every host checks generated bindings below. Unix hosts additionally compile
// the actual NuttX factory, whose descriptor and cancellation APIs require Unix.
#[cfg(unix)]
#[path = @PROBE@] pub mod probe;
#[cfg(unix)]
mod ffi {
    pub fn transfer(_: i32, _: u8, _: u32, _: &[u8], _: &mut [u8]) -> std::io::Result<()> {
        panic!("resolving sensor settings must not access the bus")
    }
    pub fn pipe() -> std::io::Result<(std::fs::File, std::fs::File)> { panic!("unexpected I/O") }
    pub fn wait(_: i32, _: i32) -> std::io::Result<bool> { panic!("unexpected I/O") }
    pub fn tick(_: i32, _: i32) -> std::io::Result<bool> { panic!("unexpected I/O") }
    pub fn irq_check(_: i32) -> std::io::Result<()> { panic!("unexpected I/O") }
    pub fn irq_register(_: i32) -> std::io::Result<()> { panic!("unexpected I/O") }
    pub fn irq_unregister(_: i32) -> std::io::Result<()> { panic!("unexpected I/O") }
    pub fn irq_read(_: i32) -> std::io::Result<bool> { panic!("unexpected I/O") }
}
#[cfg(unix)]
use ffi as events;
#[cfg(unix)]
#[path = @GPIO@] mod gpio_resource;
#[cfg(unix)]
#[path = @RESOURCE@] mod resource;
#[cfg(unix)]
#[path = @SPI@] mod spi_resource;
#[cfg(unix)]
mod spi_ffi { pub fn transfer(_: i32, _: u16, _: u32, _: u8, _: &[u8], _: &mut [u8]) -> std::io::Result<()> { panic!("unexpected I/O") } }
#[cfg(unix)]
#[path = @FACTORY@] mod production;
#[cfg(unix)]
mod platform { pub(crate) use crate::production::open_probed; }
// Windows cannot compile the Unix descriptor implementation. Still compile
// the generated declaration/alias syntax against its typed resource contracts.
#[cfg(not(unix))]
mod production {
    mod factory {
        pub struct I2cBinding<'a> { pub path: &'a str, pub address: u8, pub frequency: u32, pub irq_path: Option<&'a str> }
        pub struct SpiBinding<'a> { pub path: &'a str, pub chip_select: u16, pub mode: u8, pub frequency: u32, pub irq_path: Option<&'a str> }
    }
    mod implementations { pub struct Bmi270; pub struct Lsm6dso; pub struct Bmi270Spi; pub struct Lsm6dsoSpi; }
    mod instance {
        pub struct Instance<S, B>(pub B, pub std::marker::PhantomData<S>);
        impl<S: Sync, B: Sync> crate::Device for Instance<S, B> {}
    }
    macro_rules! declare_device {
        ($name:ident, $id:literal, $sensor:ty, $binding_type:ty, $binding:expr, $stack:expr) => {
            static $name: instance::Instance<$sensor, $binding_type> = instance::Instance($binding, std::marker::PhantomData);
        };
    }
    include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));
}
#[cfg(unix)] include!("named.rs");
fn main() {
    #[cfg(unix)] {
        let applied = production::primary().resolve(Config {
            min_rate_millihz: 100000, max_rate_millihz: 110000,
            accel_range_g: 4, gyro_range_dps: 2000, max_delivery_interval_us: 10000,
        }).unwrap();
        assert!(matches!(applied.rate_millihz, 100000 | 104000));
        verify_named();
    }
    println!("{}", include_str!(concat!(env!("OUT_DIR"), "/imu_config.rs")));
    println!("{}", include_str!(concat!(env!("OUT_DIR"), "/gnss/gnss_config.rs")));
}

"#
        .replace(
            "@GPIO@",
            &format!("{:?}", source.join("driver/gpio/tests/support/mod.rs")),
        )
        .replace("@SPI@", &format!("{:?}", source.join("driver/spi/tests/support/mod.rs")))
        .replace("@INSTANCE@", &format!("{:?}", source.join("hal/imu/src/instance.rs")))
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
    fs::write(root.join("src/named.rs"), "fn verify_named() {}\n").unwrap();
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
    let bmi_spi = source.join("tests/nuttx-std/imu-qualification/overlays/bmi270-spi.toml");
    let lsm_spi = source.join("tests/nuttx-std/imu-qualification/overlays/lsm6dso-spi.toml");
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
    let original = [
        "implementations::Bmi270,",
        "address: 104",
        "path: \"/dev/i2c0\"",
        "path: \"/dev/ttyS1\", baud: 38400",
    ];
    for (overlays, expected) in [
        (&[][..], original),
        (
            &[patch][..],
            [
                "implementations::Bmi270,",
                "address: 105",
                "path: \"/dev/i2c1\"",
                "path: \"/dev/ttyS2\", baud: 115200",
            ],
        ),
        (
            &[replacement][..],
            [
                "implementations::Lsm6dso,",
                "address: 106",
                "frequency: 100000",
                "path: \"/dev/ttyS1\", baud: 38400",
            ],
        ),
        (
            &[bmi_spi][..],
            [
                "implementations::Bmi270Spi,",
                "chip_select: 0",
                "mode: 3",
                "path: \"/dev/spi2\"",
            ],
        ),
        (
            &[lsm_spi][..],
            [
                "implementations::Lsm6dsoSpi,",
                "chip_select: 0",
                "mode: 3",
                "path: \"/dev/spi2\"",
            ],
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
        let output = String::from_utf8_lossy(&result.stdout);
        for field in expected {
            assert!(output.contains(field), "missing {field}: {output}");
        }
        assert_eq!(output.matches("declare_device!(").count(), 1);
    }

    // Same-model plus mixed-model instances and aliases compile in the actual factory.
    let multiple = root.join("multiple.toml");
    fs::write(
        &multiple,
        r#"
[devices.backup]
compatible = "bosch,bmi270"
bus = "sensor_bus"
address = 105
[devices.backup.wake]
kind = "periodic"
[devices.auxiliary]
compatible = "st,lsm6dso"
bus = "sensor_bus"
address = 106
[devices.auxiliary.wake]
kind = "periodic"
[roles]
imu_backup = "backup"
imu_auxiliary = "auxiliary"
imu_navigation = "motion"
"#,
    )
    .unwrap();
    fs::write(root.join("src/named.rs"), r#"
fn verify_named() {
    let primary = production::primary();
    let alias = production::devices::navigation();
    assert_eq!(primary as *const _ as *const (), alias as *const _ as *const ());
    assert_eq!(production::devices::backup().id(), "backup");
    assert_eq!(production::devices::auxiliary().id(), "auxiliary");
    assert_ne!(primary as *const _ as *const (), production::devices::backup() as *const _ as *const ());
}
"#).unwrap();
    let profile = Profile::resolve_with_overlays(&sdk, "m5stack-cores3-gnss", &[multiple]).unwrap();
    resolved::write(&receipt, &sdk, &profile, "xtensa-esp32s3-nuttx", "nuttx").unwrap();
    let result = run();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&result.stdout)
            .matches("declare_device!(")
            .count(),
        3
    );
    fs::write(root.join("src/named.rs"), "fn verify_named() {}\n").unwrap();
    let profile = Profile::resolve(&sdk, "m5stack-cores3-gnss").unwrap();
    resolved::write(&receipt, &sdk, &profile, "xtensa-esp32s3-nuttx", "nuttx").unwrap();
    let result = run();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&result.stdout)
            .matches("declare_device!(")
            .count(),
        1
    );

    // Content changes at the same declared path must invalidate both Cargo and
    // the receipt, even when the resolved values are semantically unchanged.
    for relative in [
        "driver/bmi270/bindings.toml",
        "driver/lsm6dso/bindings.toml",
        "driver/bmi270/Cargo.toml",
        "hal/gnss/bindings.toml",
    ] {
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
        for field in original {
            assert!(String::from_utf8_lossy(&refreshed.stdout).contains(field));
        }
    }
}
