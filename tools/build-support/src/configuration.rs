//! Fixed resource contracts for checked-in Rust constructors.
//! Catalogs select one implementation; profiles supply values, never Rust syntax.
use crate::{
    composition::{Composition, Resource, Wake},
    Result,
};

/// Emit canonical instances, direct device accessors and aliases for one HAL.
pub fn render_imu_instances(
    graph: &Composition,
    catalogs: &crate::bindings::Catalogs,
    stack: u64,
) -> Result<String> {
    let mut source = String::from(
        "// Generated canonical instances; runtime policy remains in checked-in factories.\n",
    );
    let mut instances = std::collections::BTreeMap::new();
    for (id, device) in &graph.devices {
        if !catalogs
            .values("imu")
            .any(|b| b["compatible"] == device.compatible)
        {
            continue;
        }
        let irq = match device.wake.as_ref().ok_or("missing wake configuration")? {
            Wake::Periodic {} => None,
            Wake::Interrupt { resource } => match graph.resources.get(resource) {
                Some(Resource::GpioInterrupt { device, .. }) => Some(device.as_str()),
                _ => return Err("wake requires an interrupt resource".into()),
            },
        };
        let (bus, resource, open, config) = match graph.resources.get(&device.bus) {
            Some(Resource::I2c {
                device: path,
                frequency,
            }) => {
                let address = device.address.ok_or("I2C device requires an address")?;
                ("i2c", "I2cResource", "open_i2c", format!("(resource::Config {{ path: {path:?}, frequency: {frequency} }}, {address})"))
            }
            Some(Resource::Spi {
                device: path,
                frequency,
            }) => {
                let chip_select = device
                    .chip_select
                    .ok_or("SPI device requires chip-select")?;
                let mode = device.spi_mode.ok_or("SPI device requires spi-mode")?;
                ("spi", "SpiResource", "open_spi", format!("spi_resource::Config {{ path: {path:?}, chip_select: {chip_select}, frequency: {frequency}, mode: {mode} }}"))
            }
            _ => return Err("IMU requires an I2C or SPI bus".into()),
        };
        let binding = catalogs.binding("imu", &device.compatible, bus)?;
        let implementation = binding["implementation"]
            .as_str()
            .ok_or("missing implementation")?;
        crate::bindings::validate_implementation(implementation)?;
        let name = format!("DEVICE_{}", instances.len());
        source.push_str(&format!("declare_device!({name}, {id:?}, implementations::{implementation}, resources::{resource}, resources::{open}, {config}, {irq:?}, {stack});\n"));
        instances.insert(id.as_str(), (name, implementation, resource));
    }
    source.push_str("pub mod devices {\n");
    for (accessor, id) in graph.imu_accessors(catalogs)? {
        let (instance, _, _) = instances
            .get(id)
            .ok_or("IMU accessor references an inactive device")?;
        source.push_str(&format!(
            "pub fn r#{accessor}() -> &'static impl imu::Device {{ &super::{instance} }}\n"
        ));
    }
    source.push_str("}\n");
    let id = graph
        .aliases
        .get("imu")
        .ok_or("missing primary IMU alias")?;
    let (instance, implementation, resources) = instances
        .get(id.as_str())
        .ok_or("missing primary IMU instance")?;
    source.push_str(&format!("fn primary_instance() -> &'static Instance<implementations::{implementation}, resources::{resources}> {{ &{instance} }}\n"));
    Ok(source)
}

/// Emit each UART receiver once; aliases borrow the canonical instance.
pub fn render_gnss_instances(
    graph: &Composition,
    catalogs: &crate::bindings::Catalogs,
    stack: u64,
) -> Result<String> {
    let mut source = String::from("// Generated canonical GNSS instances.\n");
    let mut instances = std::collections::BTreeMap::new();
    for (id, device) in &graph.devices {
        if !catalogs
            .values("gnss")
            .any(|b| b["compatible"] == device.compatible)
        {
            continue;
        }
        let Some(Resource::Uart { device: path, baud }) = graph.resources.get(&device.bus) else {
            return Err("GNSS requires a UART bus".into());
        };
        let binding = catalogs.binding("gnss", &device.compatible, "uart")?;
        let implementation = binding["implementation"]
            .as_str()
            .ok_or("missing implementation")?;
        crate::bindings::validate_implementation(implementation)?;
        let name = format!("DEVICE_{}", instances.len());
        source.push_str(&format!("declare_device!({name}, {id:?}, implementations::{implementation}, {path:?}, {baud}, {stack});\n"));
        instances.insert(id.as_str(), name);
    }
    source.push_str("pub mod devices {\n");
    for (accessor, id) in graph.gnss_accessors(catalogs)? {
        let instance = instances
            .get(id)
            .ok_or("GNSS accessor references an inactive device")?;
        source.push_str(&format!(
            "pub fn r#{accessor}() -> &'static impl gnss::Device {{ &super::{instance} }}\n"
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
        let graph = profile.composition().unwrap().unwrap();
        let mut package =
            crate::bindings::Package::load(&source.join("platform/devices/imu")).unwrap();
        for input in &mut package.catalogs {
            input.contents = input.contents.replace("Bmi270", "AnotherSensor");
            input.sha256 = crate::hash_bytes(input.contents.as_bytes());
        }
        let mut catalogs = crate::bindings::Catalogs::default();
        catalogs.insert("imu", package).unwrap();
        let imu = render_imu_instances(&graph, &catalogs, 16384).unwrap();
        assert!(imu.contains("implementations::AnotherSensor"));
        assert!(imu
            .contains("(resource::Config { path: \"/dev/i2c0\", frequency: 400000 }, 104), None"));
        let gnss = render_gnss_instances(&graph, profile.catalogs(), 32768).unwrap();
        assert!(gnss.contains("implementations::NeoM9n, \"/dev/ttyS1\", 38400, 32768"));
        for invalid in [
            "",
            "Self",
            "super::Sensor",
            "Sensor;fn injected(){}",
            "1Sensor",
        ] {
            assert!(crate::bindings::validate_implementation(invalid).is_err());
        }

        // Compile both output contracts without hardware or target dependencies.
        let temp = TempDir::new(&std::env::temp_dir()).unwrap();
        let witness = temp.0.join("witness.rs");
        fs::write(&witness, format!(r#"
            #![allow(dead_code, unused_imports)]
            extern crate self as imu;
            extern crate self as gnss;
            trait Device {{}}
            mod imu_wiring {{
                mod implementations {{ pub struct AnotherSensor; }}
                mod resource {{ pub struct Config<'a> {{ pub path: &'a str, pub frequency: u32 }} }}
                mod resources {{
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
                mod implementations {{ pub struct NeoM9n; }}
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
            .args(["--edition=2021", "--emit=metadata"])
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
