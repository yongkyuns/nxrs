//! Fixed resource contracts for checked-in Rust constructors.
//! Catalogs select one implementation; profiles supply values, never Rust syntax.
use crate::{
    composition::{Composition, Resource, Wake},
    Result,
};

pub fn render(
    graph: &Composition,
    role: &str,
    implementation: &str,
    stack_bytes: u64,
) -> Result<String> {
    crate::bindings::validate_implementation(implementation)?;
    let id = graph.roles.get(role).ok_or("missing device role")?;
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
    let (binding_type, fields) = match bus {
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
    Ok(format!(
        "// Generated from validated device/resource bindings.\n\
         use implementations::{implementation} as Implementation;\n\
         const STACK_BYTES: usize = {stack_bytes};\n\
         const BINDING: {binding_type}<'static> = {binding_type} {{ {fields} }};\n"
    ))
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
        let imu = render(&graph, "imu", "AnotherSensor", 16384).unwrap();
        assert!(imu.contains("use implementations::AnotherSensor as Implementation;"));
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
            mod imu {{
                mod implementations {{ pub struct AnotherSensor; }}
                mod factory {{ pub struct I2cBinding<'a> {{ pub path: &'a str, pub address: u8, pub frequency: u32, pub irq_path: Option<&'a str> }} }}
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
