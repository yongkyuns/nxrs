//! Package-owned declarative configuration, independent of Cargo's environment.
use crate::{
    bindings::{Catalogs, Package},
    configuration,
    profile::Profile,
    Result, TempDir,
};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
};

/// Generate the selected provider's configuration and return every source input.
///
/// The caller supplies an owned build-output directory and validates the complete
/// compiler target before invoking this OS-specific preparation. This function
/// does not read process environment variables, emit Cargo directives, compile
/// native helpers, or select dependencies.
pub fn prepare_package(
    directory: &Path,
    profile: &Profile,
    os: &str,
    out: &Path,
) -> Result<Vec<PathBuf>> {
    profile.validate_target(os)?;
    let package = Package::load(directory)?;
    let name = package.name.clone();
    let capability = name
        .strip_prefix("nxrs-")
        .filter(|name| {
            !name.is_empty()
                && name
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
        })
        .ok_or("configuration package must have an nxrs- capability name")?;
    let mut inputs: Vec<_> = package.inputs().map(|input| input.path.clone()).collect();
    inputs.extend(profile.source_paths().cloned());
    inputs.extend(profile.catalogs().source_paths().cloned());
    inputs.sort();
    inputs.dedup();

    let provider = profile.binding(capability);
    if provider != os || package.catalogs.is_empty() {
        publish(out, &name, None)?;
        return Ok(inputs);
    }
    let expected = profile
        .catalogs()
        .package(capability)
        .ok_or_else(|| format!("profile has no declared package bindings for {capability}"))?;
    if !same_declarations(&package, expected) {
        return Err(format!(
            "configuration inputs for {name} differ from the resolved profile; use matching package binding catalogs"
        ));
    }

    let composition = profile
        .composition()?
        .ok_or_else(|| format!("{name}/{provider} configuration requires a composition"))?;
    let device_id = composition
        .roles
        .get(capability)
        .ok_or_else(|| format!("{name}: missing device role {capability}"))?;
    let device = composition
        .devices
        .get(device_id)
        .ok_or_else(|| format!("{name}: missing device {device_id}"))?;
    let resource = composition
        .resources
        .get(&device.bus)
        .ok_or_else(|| format!("{name}: missing bus {}", device.bus))?;
    let bus = match resource {
        crate::composition::Resource::I2c { .. } => "i2c",
        crate::composition::Resource::Spi { .. } => "spi",
        crate::composition::Resource::Uart { .. } => "uart",
        _ => return Err("device bus must be I2C, SPI or UART".into()),
    };
    let mut catalogs = Catalogs::default();
    catalogs.insert(capability, package)?;
    let binding = catalogs.binding(capability, &device.compatible, bus)?;
    let implementation = binding["implementation"]
        .as_str()
        .ok_or("missing implementation")?;
    let stack = crate::profile::sensor_stack(&profile.data[capability])?;
    let source = configuration::render(&composition, capability, implementation, stack)?;
    publish(
        out,
        &name,
        Some((&format!("{capability}_config.rs"), &source)),
    )?;
    Ok(inputs)
}

/// Detached packages may adjust packaging/dependency metadata. Their declared
/// build data must still match the inputs against which the profile was resolved.
fn same_declarations(actual: &Package, expected: &Package) -> bool {
    actual.name == expected.name
        && actual
            .catalogs
            .iter()
            .map(|input| &input.sha256)
            .eq(expected.catalogs.iter().map(|input| &input.sha256))
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct WrittenOutput {
    version: u32,
    file: String,
}

fn output_name(name: &str) -> bool {
    name.len() > 3
        && name.ends_with(".rs")
        && !name.starts_with('.')
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

/// Keep cleanup limited to the one Rust file recorded for this package. Cargo's
/// OUT_DIR persists, so removing a binding must not leave an old include
/// available to an unchanged runtime source file.
fn publish(out: &Path, package: &str, generated: Option<(&str, &str)>) -> Result<()> {
    let receipt = out.join(format!("{package}.configuration.json"));
    let previous = match fs::read(&receipt) {
        Ok(bytes) => {
            let value: WrittenOutput = serde_json::from_slice(&bytes)
                .map_err(|error| format!("{}: {error}", receipt.display()))?;
            if value.version != 1 || !output_name(&value.file) {
                return Err(format!(
                    "invalid generated-output record {}",
                    receipt.display()
                ));
            }
            Some(value.file)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(format!("{}: {error}", receipt.display())),
    };
    if let Some((name, source)) = generated {
        if !output_name(name) {
            return Err("generated configuration output must be a Rust file basename".into());
        }
        fs::create_dir_all(out).map_err(|error| error.to_string())?;
        write_if_changed(&out.join(name), source)?;
    }
    let current = generated.map(|(name, _)| name);
    if let Some(previous) = previous.as_deref().filter(|name| Some(*name) != current) {
        crate::remove_file(&out.join(previous))?;
    }
    if let Some(file) = current {
        let value = WrittenOutput {
            version: 1,
            file: file.to_owned(),
        };
        let text = serde_json::to_string(&value).map_err(|error| error.to_string())? + "\n";
        write_if_changed(&receipt, &text)?;
    } else {
        crate::remove_file(&receipt)?;
    }
    Ok(())
}

fn write_if_changed(path: &Path, contents: &str) -> Result<()> {
    if fs::read(path).ok().as_deref() == Some(contents.as_bytes()) {
        return Ok(());
    }
    let parent = path.parent().ok_or("generated output has no parent")?;
    let temporary = TempDir::new(parent)?;
    let fresh = temporary.0.join("output");
    fs::write(&fresh, contents).map_err(|error| format!("{}: {error}", path.display()))?;
    fs::rename(fresh, path).map_err(|error| format!("{}: {error}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn output_rename_and_removal_cannot_reuse_stale_configuration() {
        let temporary = TempDir::new(&std::env::temp_dir()).unwrap();
        let out = temporary.0.join("generated");
        publish(
            &out,
            "nxrs-fixture",
            Some(("first.rs", "const VALUE: u32 = 1;\n")),
        )
        .unwrap();
        fs::write(out.join("helper.a"), "native archive").unwrap();
        fs::write(out.join("unrelated.rs"), "other generator").unwrap();
        let modified = fs::metadata(out.join("first.rs"))
            .unwrap()
            .modified()
            .unwrap();
        publish(
            &out,
            "nxrs-fixture",
            Some(("first.rs", "const VALUE: u32 = 1;\n")),
        )
        .unwrap();
        assert_eq!(
            modified,
            fs::metadata(out.join("first.rs"))
                .unwrap()
                .modified()
                .unwrap()
        );

        publish(
            &out,
            "nxrs-fixture",
            Some(("second.rs", "const VALUE: u32 = 2;\n")),
        )
        .unwrap();
        assert!(!out.join("first.rs").exists());
        assert_eq!(
            fs::read_to_string(out.join("second.rs")).unwrap(),
            "const VALUE: u32 = 2;\n"
        );
        publish(&out, "nxrs-fixture", None).unwrap();
        assert!(!out.join("second.rs").exists());
        assert!(!out.join("nxrs-fixture.configuration.json").exists());
        assert_eq!(
            fs::read_to_string(out.join("helper.a")).unwrap(),
            "native archive"
        );
        assert_eq!(
            fs::read_to_string(out.join("unrelated.rs")).unwrap(),
            "other generator"
        );
    }

    #[test]
    fn an_invalid_output_record_cannot_remove_another_file() {
        let temporary = TempDir::new(&std::env::temp_dir()).unwrap();
        let out = temporary.0.join("generated");
        fs::create_dir(&out).unwrap();
        let other = temporary.0.join("other.rs");
        fs::write(&other, "keep").unwrap();
        fs::write(
            out.join("nxrs-fixture.configuration.json"),
            r#"{"version":1,"file":"../other.rs"}"#,
        )
        .unwrap();
        assert!(publish(&out, "nxrs-fixture", Some(("new.rs", "unused"))).is_err());
        assert!(!out.join("new.rs").exists());
        assert_eq!(fs::read_to_string(&other).unwrap(), "keep");
    }
}
