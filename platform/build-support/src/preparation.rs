//! Package-owned declarative configuration, independent of Cargo's environment.
use crate::{
    bindings::{Catalogs, Package},
    configuration::{self, Context, Schema},
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
    let mut inputs = vec![package.manifest.path.clone()];
    inputs.extend(package.catalogs.iter().map(|input| input.path.clone()));
    inputs.extend(package.configuration.values().map(|input| input.path.clone()));
    inputs.extend(profile.source_paths().cloned());
    inputs.extend(profile.catalogs().source_paths().cloned());
    inputs.sort();
    inputs.dedup();

    let provider = profile.binding(capability);
    let Some(input) = package.configuration.get(provider) else {
        publish(out, &name, None)?;
        return Ok(inputs);
    };
    let schema_path = input.path.clone();
    let schema = Schema::parse(&input.contents)
        .map_err(|error| format!("{}: {error}", schema_path.display()))?;
    let expected = profile
        .catalogs()
        .package(capability)
        .ok_or_else(|| format!("profile has no declared package bindings for {capability}"))?;
    if !same_declarations(&package, expected) {
        return Err(format!(
            "configuration inputs for {name} differ from the resolved profile; use matching package catalogs and schemas"
        ));
    }

    let composition = profile
        .composition()?
        .ok_or_else(|| format!("{name}/{provider} configuration requires a composition"))?;
    let graph = serde_json::to_value(composition).map_err(|error| error.to_string())?;
    let role = schema.role();
    let device_id = graph["roles"]
        .get(role)
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| format!("{}: missing device role {role}", schema_path.display()))?;
    let device = graph["devices"]
        .get(device_id)
        .ok_or_else(|| format!("{}: missing device {device_id}", schema_path.display()))?;
    let compatible = device["compatible"]
        .as_str()
        .ok_or("resolved device has no compatible identity")?;
    let bus = device["bus"]
        .as_str()
        .ok_or("resolved device has no bus reference")?;
    let resources = &graph["resources"];
    let bus_kind = resources
        .get(bus)
        .and_then(|resource| resource.get("kind"))
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| format!("resolved device {device_id} has no bus resource {bus}"))?;
    let mut catalogs = Catalogs::default();
    catalogs.insert(capability, package)?;
    let binding = catalogs.binding(capability, compatible, bus_kind)?;
    let source = configuration::render(
        &schema,
        &Context {
            profile: &profile.data,
            device,
            resources,
            binding,
        },
    )
    .map_err(|error| format!("{}: {error}", schema_path.display()))?;
    publish(out, &name, Some((schema.output(), &source)))?;
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
        && actual
            .configuration
            .iter()
            .map(|(provider, input)| (provider, &input.sha256))
            .eq(
                expected
                    .configuration
                    .iter()
                    .map(|(provider, input)| (provider, &input.sha256)),
            )
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
/// OUT_DIR persists, so removing/renaming a schema must not leave an old include
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
