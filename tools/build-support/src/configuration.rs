//! Fixed resource contracts for checked-in Rust constructors.
//! Catalogs select one implementation; profiles supply values, never Rust syntax.
use crate::{
    composition::{Composition, Resource, Wake},
    Result,
};

/// Emit one instance per selected device, with aliases borrowing the same object.
pub(crate) fn render_instances(
    graph: &Composition,
    catalogs: &crate::bindings::Catalogs,
    capability: &str,
    stack: u64,
) -> Result<String> {
    let mut source = String::from("// Generated canonical device instances.\n");
    let mut instances = std::collections::BTreeMap::new();
    let primary = graph.aliases.get(capability);
    for (id, device) in graph.devices_for(catalogs, capability) {
        let irq = match &device.wake {
            Some(Wake::Interrupt { resource }) => match graph.resources.get(resource) {
                Some(Resource::GpioInterrupt { device, .. }) => Some(device.as_str()),
                _ => return Err("wake requires an interrupt resource".into()),
            },
            Some(Wake::Periodic {}) | None => None,
        };
        let (bus, adapter, arguments) = match graph.resources.get(&device.bus) {
            Some(Resource::I2c {
                device: path,
                frequency,
            }) => {
                let address = device.address.ok_or("I2C device requires an address")?;
                let config =
                    format!("resource::Config {{ path: {path:?}, frequency: {frequency} }}");
                ("i2c", "resources::Bus", format!("resources::I2cResource, resources::open_i2c, ({config}, {address}), {irq:?}"))
            }
            Some(Resource::Spi {
                device: path,
                frequency,
            }) => {
                let chip_select = device
                    .chip_select
                    .ok_or("SPI device requires chip-select")?;
                let mode = device.spi_mode.ok_or("SPI device requires spi-mode")?;
                let config = format!(
                    "spi_resource::Config {{ path: {path:?}, chip_select: {chip_select}, frequency: {frequency}, mode: {mode} }}"
                );
                (
                    "spi",
                    "resources::Spi",
                    format!("resources::SpiResource, resources::open_spi, {config}, {irq:?}"),
                )
            }
            Some(Resource::Uart { device: path, baud }) => {
                ("uart", "Serial", format!("{path:?}, {baud}"))
            }
            _ => return Err(format!("device {id} requires a supported bus")),
        };
        let binding = catalogs.binding(capability, &device.compatible, bus)?;
        let implementation = binding["implementation"]
            .as_str()
            .ok_or("missing implementation")?;
        crate::bindings::validate_implementation(implementation)?;
        let driver = catalogs
            .package(capability)
            .and_then(|package| package.drivers.get(&device.compatible))
            .ok_or_else(|| format!("{} has no Cargo driver registration", device.compatible))?;
        let dependency = driver.dependency.replace('-', "_");
        let delay = if driver.stores_delay {
            ", resources::Delay"
        } else {
            ""
        };
        let driver_type = format!("::r#{dependency}::{implementation}<{adapter}{delay}>");
        let name = if primary.is_some_and(|primary| primary == id) {
            "PRIMARY_DEVICE".to_owned()
        } else {
            format!("DEVICE_{}", instances.len())
        };
        source.push_str(&format!(
            "declare_device!({name}, {id:?}, {driver_type}, {arguments}, {stack});\n"
        ));
        if capability == "imu" && name == "PRIMARY_DEVICE" {
            let enable_reference = if driver.sensor_reference {
                "Some(|sensor| sensor.enable_sensor_reference().map_err(Into::into))"
            } else {
                "None"
            };
            source.push_str(&format!(
                "#[cfg(feature = \"probe\")]\nconst SENSOR_REFERENCE: Option<fn(&mut {driver_type}) -> Result<(), peripheral::imu::Error>> = {enable_reference};\n"
            ));
        }
        instances.insert(id, name);
    }

    source.push_str("pub mod devices {\n");
    for (accessor, id) in graph.accessors(catalogs, capability)? {
        let instance = instances
            .get(id)
            .ok_or("accessor references an inactive device")?;
        source.push_str(&format!(
            "pub fn r#{accessor}() -> &'static impl super::Device {{ &super::{instance} }}\n"
        ));
    }
    source.push_str("}\n");
    Ok(source)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{profile::Profile, TempDir};
    use std::{fs, path::Path};

    #[test]
    fn resources_compile_with_a_new_implementation_without_a_model_table() {
        let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let profile = Profile::resolve(&source, "m5stack-cores3-gnss").unwrap();
        let mut graph = profile.composition().unwrap().unwrap();
        graph.devices.get_mut("imu").unwrap().compatible = "vendor,fixture".into();
        let temp = TempDir::new(&std::env::temp_dir()).unwrap();
        let composition = temp.0.join("composition");
        let driver = temp.0.join("driver");
        fs::create_dir(&composition).unwrap();
        fs::create_dir(&driver).unwrap();
        fs::write(
            composition.join("Cargo.toml"),
            r#"
            [package]
            name = "fixture-composition"
            [features]
            imu = []
            [package.metadata.nxrs]
            device-capabilities = ["imu"]
            [target.'cfg(target_os="nuttx")'.dependencies]
            test-imu = { package = "fixture-driver", path = "../driver", optional = true }
        "#,
        )
        .unwrap();
        fs::write(
            driver.join("Cargo.toml"),
            r#"
            [package]
            name = "fixture-driver"
            [package.metadata.nxrs]
            bindings = ["bindings.toml"]
            [package.metadata.nxrs.device]
            capability = "imu"
        "#,
        )
        .unwrap();
        fs::write(
            driver.join("bindings.toml"),
            r#"
            version = 2
            [[binding]]
            compatible = "vendor,fixture"
            bus = "i2c"
            implementation = "AnotherSensor"
        "#,
        )
        .unwrap();
        let mut catalogs = crate::bindings::Catalogs::default();
        catalogs
            .insert(
                "imu",
                crate::bindings::Package::load_capability(&composition, "imu").unwrap(),
            )
            .unwrap();
        let imu = render_instances(&graph, &catalogs, "imu", 16384).unwrap();
        assert!(imu.contains("::r#test_imu::AnotherSensor<resources::Bus>"));
        assert!(imu.contains("peripheral::imu::Error>> = None;"));
        assert!(imu
            .contains("(resource::Config { path: \"/dev/i2c0\", frequency: 400000 }, 104), None"));
        let gnss = render_instances(&graph, profile.catalogs(), "gnss", 32768).unwrap();
        assert!(gnss.contains("::r#ubx::NeoM9n<Serial>, \"/dev/ttyS1\", 38400, 32768"));
        for invalid in [
            "",
            "Self",
            "super::Sensor",
            "Sensor;fn injected(){}",
            "1Sensor",
        ] {
            assert!(crate::bindings::validate_implementation(invalid).is_err());
        }

        let manifest = composition.join("Cargo.toml");
        let registration = fs::read_to_string(&manifest).unwrap();
        for (contents, message) in [
            (
                registration.replace("test-imu =", "default ="),
                "implicit Cargo feature",
            ),
            (
                format!(
                    "{registration}\n{}",
                    registration
                        .lines()
                        .find(|line| line.contains("test-imu ="))
                        .unwrap()
                        .replace("test-imu", "test_imu")
                ),
                "collides",
            ),
        ] {
            fs::write(&manifest, contents).unwrap();
            assert!(
                crate::bindings::Package::load_capability(&composition, "imu")
                    .unwrap_err()
                    .contains(message)
            );
        }

        // Compile both output contracts without hardware or target dependencies.
        let witness = temp.0.join("witness.rs");
        fs::write(&witness, format!(r#"
            #![allow(dead_code, unused_imports)]
            extern crate self as peripheral;
            extern crate self as test_imu;
            extern crate self as ubx;
            pub mod imu {{ pub struct Error; }}
            pub struct AnotherSensor<B>(std::marker::PhantomData<B>);
            pub struct NeoM9n<R>(std::marker::PhantomData<R>);
            trait Device {{}}
            mod imu_wiring {{
                use crate::Device;
                mod resource {{ pub struct Config<'a> {{ pub path: &'a str, pub frequency: u32 }} }}
                mod resources {{
                    pub type Bus = ();
                    pub type I2cResource = ();
                    pub fn open_i2c(_: (super::resource::Config<'_>, u8), _: ()) -> I2cResource {{}}
                }}
                struct Instance<S, R>(std::marker::PhantomData<(S, R)>);
                impl<S, R> crate::Device for Instance<S, R> {{}}
                macro_rules! declare_device {{
                    ($name:ident, $id:literal, $sensor:ty, $resources:ty, $open:path, $value:expr, $irq:expr, $stack:literal) => {{
                        const _: fn() = || {{ let _: $resources = $open($value, ()); let _: Option<&str> = $irq; }};
                        static $name: Instance<$sensor, $resources> = Instance(std::marker::PhantomData);
                    }};
                }}
                {imu}
            }}
            mod gnss_wiring {{
                use crate::Device;
                type Serial = ();
                struct Instance<R>(std::marker::PhantomData<R>);
                impl<R> crate::Device for Instance<R> {{}}
                macro_rules! declare_device {{
                    ($name:ident, $id:literal, $receiver:ty, $path:literal, $baud:literal, $stack:literal) => {{
                        static $name: Instance<$receiver> = Instance(std::marker::PhantomData);
                    }};
                }}
                {gnss}
            }}
            fn main() {{}}
        "#)).unwrap();
        let result = std::process::Command::new("rustc")
            .args(["--edition=2021", "--emit=metadata", "--cfg", "feature=\"probe\""])
            .arg(&witness)
            .arg("-o")
            .arg(temp.0.join("witness.rmeta"))
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
    }
}
