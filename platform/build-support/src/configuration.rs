//! Fixed resource contracts for checked-in Rust constructors.
//! Catalogs select one implementation; profiles supply values, never Rust syntax.
use crate::{
    composition::{Composition, Resource, Wake},
    Result,
};

pub fn render(
    graph: &Composition,
    alias: &str,
    implementation: &str,
    stack_bytes: u64,
) -> Result<String> {
    crate::bindings::validate_implementation(implementation)?;
    let id = graph.aliases.get(alias).ok_or("missing device alias")?;
    let (binding_type, fields) = resource_binding(graph, id)?;
    Ok(format!(
        "// Generated from validated device/resource bindings.\n\
         use implementations::{implementation} as Implementation;\n\
         const STACK_BYTES: usize = {stack_bytes};\n\
         const BINDING: {binding_type}<'static> = {binding_type} {{ {fields} }};\n"
    ))
}

fn resource_binding(graph: &Composition, id: &str) -> Result<(&'static str, String)> {
    let device = graph.devices.get(id).ok_or("missing device")?;
    let bus = graph
        .resources
        .get(&device.bus)
        .ok_or("missing device bus")?;
    let irq_path = || -> Result<Option<&str>> {
        match device.wake.as_ref().ok_or("missing wake configuration")? {
            Wake::Periodic {} => Ok(None),
            Wake::Interrupt { resource } => match graph.resources.get(resource) {
                Some(Resource::GpioInterrupt { device, .. }) => Ok(Some(device.as_str())),
                _ => Err("wake requires an interrupt resource".into()),
            },
        }
    };
    let binding = match bus {
        Resource::I2c {
            device: path,
            frequency,
        } => {
            let address = device.address.ok_or("I2C device requires an address")?;
            let irq_path = irq_path()?;
            ("factory::I2cBinding", format!("path: {path:?}, address: {address}, frequency: {frequency}, irq_path: {irq_path:?}"))
        }
        Resource::Spi {
            device: path,
            frequency,
        } => {
            let chip_select = device
                .chip_select
                .ok_or("SPI device requires chip-select")?;
            let mode = device.spi_mode.ok_or("SPI device requires spi-mode")?;
            let irq_path = irq_path()?;
            ("factory::SpiBinding", format!("path: {path:?}, chip_select: {chip_select}, frequency: {frequency}, mode: {mode}, irq_path: {irq_path:?}"))
        }
        Resource::Uart { device: path, baud } => {
            ("Binding", format!("path: {path:?}, baud: {baud}"))
        }
        Resource::GpioInterrupt { .. } => return Err("interrupt resource is not a bus".into()),
    };
    Ok(binding)
}

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
        let (binding_type, fields) = resource_binding(graph, id)?;
        let bus = match graph.resources.get(&device.bus) {
            Some(Resource::I2c { .. }) => "i2c",
            Some(Resource::Spi { .. }) => "spi",
            _ => return Err("IMU requires an I2C or SPI bus".into()),
        };
        let binding = catalogs.binding("imu", &device.compatible, bus)?;
        let implementation = binding["implementation"]
            .as_str()
            .ok_or("missing implementation")?;
        crate::bindings::validate_implementation(implementation)?;
        let name = format!("DEVICE_{}", instances.len());
        source.push_str(&format!("declare_device!({name}, {id:?}, implementations::{implementation}, {binding_type}<'static>, {binding_type} {{ {fields} }}, {stack});\n"));
        instances.insert(id.as_str(), (name, implementation, binding_type));
    }
    source.push_str("pub mod devices {\n");
    for (accessor, id) in graph.imu_accessors(catalogs)? {
        let (instance, _, _) = instances
            .get(id)
            .ok_or("IMU accessor references an inactive device")?;
        source.push_str(&format!(
            "pub fn r#{accessor}() -> &'static impl crate::Device {{ &super::{instance} }}\n"
        ));
    }
    source.push_str("}\npub use devices::primary;\n");
    let id = graph
        .aliases
        .get("imu")
        .ok_or("missing primary IMU alias")?;
    let (instance, implementation, binding_type) = instances
        .get(id.as_str())
        .ok_or("missing primary IMU instance")?;
    source.push_str(&format!("fn primary_instance() -> &'static instance::Instance<implementations::{implementation}, {binding_type}<'static>> {{ &{instance} }}\n"));
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
        let mut package = crate::bindings::Package::load(&source.join("hal/imu")).unwrap();
        for input in &mut package.catalogs {
            input.contents = input.contents.replace("Bmi270", "AnotherSensor");
            input.sha256 = crate::hash_bytes(input.contents.as_bytes());
        }
        let mut catalogs = crate::bindings::Catalogs::default();
        catalogs.insert("imu", package).unwrap();
        let imu = render_imu_instances(&graph, &catalogs, 16384).unwrap();
        assert!(imu.contains("implementations::AnotherSensor"));
        assert!(imu.contains("address: 104, frequency: 400000, irq_path: None"));
        let gnss = render(&graph, "gnss", "NeoM9n", 32768).unwrap();
        assert!(gnss.contains("path: \"/dev/ttyS1\", baud: 38400"));
        assert!(gnss.contains("STACK_BYTES: usize = 32768"));
        for invalid in [
            "",
            "Self",
            "super::Sensor",
            "Sensor;fn injected(){}",
            "1Sensor",
        ] {
            assert!(render(&graph, "imu", invalid, 16384).is_err());
        }
        assert!(render(&graph, "unknown", "Sensor", 16384).is_err());

        // Compile both output contracts without hardware or target dependencies.
        let temp = TempDir::new(&std::env::temp_dir()).unwrap();
        let witness = temp.0.join("witness.rs");
        fs::write(&witness, format!(r#"
            #![allow(dead_code, unused_imports)]
            trait Device {{}}
            mod imu {{
                mod implementations {{ pub struct AnotherSensor; }}
                mod factory {{ pub struct I2cBinding<'a> {{ pub path: &'a str, pub address: u8, pub frequency: u32, pub irq_path: Option<&'a str> }} }}
                mod instance {{
                    pub struct Instance<S, B>(pub std::marker::PhantomData<S>, pub B);
                    impl<S, B> crate::Device for Instance<S, B> {{}}
                }}
                macro_rules! declare_device {{
                    ($name:ident, $id:literal, $sensor:ty, $binding:ty, $value:expr, $stack:literal) => {{
                        static $name: instance::Instance<$sensor, $binding> = instance::Instance(std::marker::PhantomData, $value);
                    }};
                }}
                {imu}
            }}
            mod gnss {{
                mod implementations {{ pub struct NeoM9n; }}
                struct Binding<'a> {{ path: &'a str, baud: u32 }}
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
