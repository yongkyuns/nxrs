//! Package-owned declarative configuration, independent of Cargo's environment.
use crate::{
    bindings::{device_capabilities, Package},
    configuration, hash_bytes,
    profile::Profile,
    Result, TempDir,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

pub const ENVIRONMENT: &str = "NXRS_PREPARED_CONFIGURATION";

/// Generate the selected provider's configuration and return every source input.
///
/// The caller supplies an owned build-output directory and validates the complete
/// compiler target before invoking this OS-specific preparation. This function
/// does not read process environment variables, emit Cargo directives, compile
/// native helpers, or select dependencies.
pub fn prepare_package(
    directory: &Path,
    capability: &str,
    profile: &Profile,
    os: &str,
    out: &Path,
) -> Result<Vec<PathBuf>> {
    let package = Package::load_capability(directory, capability)?;
    let generated = render(&package, capability, profile, os)?;
    publish(
        out,
        &format!("{}-{capability}", package.name),
        generated
            .as_ref()
            .map(|(file, source)| (file.as_str(), source.as_str())),
    )?;
    Ok(inputs(&package, profile))
}

fn inputs(package: &Package, profile: &Profile) -> Vec<PathBuf> {
    let mut inputs: Vec<_> = package.inputs().map(|input| input.path.clone()).collect();
    inputs.extend(profile.source_paths().cloned());
    inputs.sort();
    inputs.dedup();
    inputs
}

fn render(
    package: &Package,
    capability: &str,
    profile: &Profile,
    os: &str,
) -> Result<Option<(String, String)>> {
    profile.validate_target(os)?;
    let name = &package.name;
    let provider = profile.binding(capability);
    if os != "nuttx" || provider != os {
        return Ok(None);
    }
    let expected = profile
        .catalogs()
        .package(capability)
        .ok_or_else(|| format!("profile has no declared package bindings for {capability}"))?;
    if !same_declarations(package, expected)? {
        return Err(format!(
            "configuration inputs for {name} differ from the resolved profile; use matching package binding catalogs"
        ));
    }

    let composition = profile
        .composition()?
        .ok_or_else(|| format!("{name}/{provider} configuration requires a composition"))?;
    let stack = crate::profile::sensor_stack(&profile.data[capability])?;
    let source =
        configuration::render_instances(&composition, profile.catalogs(), capability, stack)?;
    Ok(Some((format!("{capability}_config.rs"), source)))
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Prepared {
    identity: String,
    output: Option<Generated>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Generated {
    file: String,
    sha256: String,
}

fn identity(
    directory: &Path,
    package: &Package,
    capability: &str,
    profile: &Profile,
    target: &str,
    os: &str,
) -> Result<String> {
    profile.validate_cargo_target(target, os)?;
    let generator = hash_bytes(
        concat!(
            env!("CARGO_PKG_VERSION"),
            include_str!("preparation.rs"),
            include_str!("configuration.rs"),
            include_str!("bindings.rs"),
            include_str!("composition.rs"),
            include_str!("profile.rs"),
            include_str!("nuttx_resources.rs"),
        )
        .as_bytes(),
    );
    let sources: Vec<_> = package
        .inputs()
        .map(|input| (&input.path, &input.sha256))
        .collect();
    Ok(hash_bytes(
        &serde_json::to_vec(&(
            directory,
            capability,
            sources,
            profile.json()?,
            target,
            os,
            generator,
        ))
        .map_err(|error| error.to_string())?,
    ))
}

/// Render consumed path catalogues before Cargo and publish an immutable handoff.
pub fn prepare_consumers<'a>(
    consumers: impl IntoIterator<Item = (&'a Path, &'a BTreeSet<String>)>,
    profile: &Profile,
    target: &str,
    os: &str,
    out: &Path,
) -> Result<Option<PathBuf>> {
    let out = std::path::absolute(out).map_err(|error| error.to_string())?;
    reject_symlink(&out)?;
    let mut selected = BTreeMap::new();
    for (directory, features) in consumers {
        let manifest = crate::profile::manifest(&directory.join("Cargo.toml"))?;
        let Some(declarations) = device_capabilities(&manifest)? else {
            continue;
        };
        let directory = directory
            .canonicalize()
            .map_err(|error| error.to_string())?;
        let mut capabilities = BTreeMap::new();
        for capability in declarations
            .into_iter()
            .filter(|name| features.contains(*name))
        {
            let package = Package::load_capability(&directory, capability)?;
            let identity = identity(&directory, &package, capability, profile, target, os)?;
            let generated = render(&package, capability, profile, os)?;
            let output = if let Some((name, source)) = generated {
                let bucket = out.join(&identity);
                reject_symlink(&bucket)?;
                let path = bucket.join(&name);
                reject_symlink(&path)?;
                fs::create_dir_all(&bucket).map_err(|error| error.to_string())?;
                write_if_changed(&path, &source)?;
                Some(Generated {
                    file: name,
                    sha256: hash_bytes(source.as_bytes()),
                })
            } else {
                None
            };
            capabilities.insert(capability.to_owned(), Prepared { identity, output });
        }
        if !capabilities.is_empty() {
            selected.insert(directory, capabilities);
        }
    }
    if selected.is_empty() {
        return Ok(None);
    }
    let text = serde_json::to_string(&selected).map_err(|error| error.to_string())?;
    fs::create_dir_all(&out).map_err(|error| error.to_string())?;
    let path = out.join(format!("{}.json", hash_bytes(text.as_bytes())));
    reject_symlink(&path)?;
    write_if_changed(&path, &text)?;
    Ok(Some(path))
}

/// Validate the selected catalogue's inputs and copy its output into Cargo's OUT_DIR.
pub fn consume(
    path: &Path,
    directory: &Path,
    capability: &str,
    profile: &Profile,
    target: &str,
    os: &str,
    out: &Path,
) -> Result<Option<Vec<PathBuf>>> {
    if !path.is_absolute() {
        return Err("prepared configuration must use an absolute handoff path".into());
    }
    reject_symlink(path.parent().ok_or("configuration handoff has no parent")?)?;
    reject_symlink(path)?;
    let directory = directory
        .canonicalize()
        .map_err(|error| error.to_string())?;
    let text = crate::read(path)?;
    if path.file_stem().and_then(|name| name.to_str()) != Some(hash_bytes(text.as_bytes()).as_str())
    {
        return Err("prepared configuration handoff changed; rerun cargo nxrs build".into());
    }
    let mut selected: BTreeMap<PathBuf, BTreeMap<String, Prepared>> =
        serde_json::from_str(&text).map_err(|error| error.to_string())?;
    let Some(prepared) = selected
        .get_mut(&directory)
        .and_then(|caps| caps.remove(capability))
    else {
        return Ok(None);
    };
    let package = Package::load_capability(&directory, capability)?;
    if prepared.identity != identity(&directory, &package, capability, profile, target, os)? {
        return Err("stale prepared configuration; rerun cargo nxrs build".into());
    }
    let mut inputs = inputs(&package, profile);
    inputs.push(path.to_path_buf());
    let generated = if let Some(generated) = &prepared.output {
        if !output_name(&generated.file) {
            return Err("invalid prepared configuration filename".into());
        }
        let source = path
            .parent()
            .ok_or("configuration handoff has no parent")?
            .join(&prepared.identity)
            .join(&generated.file);
        reject_symlink(source.parent().unwrap())?;
        reject_symlink(&source)?;
        let contents = crate::read(&source)?;
        if hash_bytes(contents.as_bytes()) != generated.sha256 {
            return Err("prepared configuration output changed; rerun cargo nxrs build".into());
        }
        inputs.push(source);
        Some((generated.file.as_str(), contents))
    } else {
        None
    };
    publish(
        out,
        &format!("{}-{capability}", package.name),
        generated
            .as_ref()
            .map(|(name, source)| (*name, source.as_str())),
    )?;
    Ok(Some(inputs))
}

fn reject_symlink(path: &Path) -> Result<()> {
    if path.is_symlink() {
        return Err(format!(
            "prepared configuration path must not be a symlink: {}",
            path.display()
        ));
    }
    Ok(())
}

/// Detached packages may adjust packaging/dependency metadata. Their declared
/// build data must still match the inputs against which the profile was resolved.
fn same_declarations(actual: &Package, expected: &Package) -> Result<bool> {
    Ok(actual.name == expected.name
        && actual.consumer_feature == expected.consumer_feature
        && actual.drivers == expected.drivers
        && actual.resource_features()? == expected.resource_features()?
        && actual
            .catalogs
            .iter()
            .map(|input| &input.sha256)
            .eq(expected.catalogs.iter().map(|input| &input.sha256)))
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
    fn detached_catalogues_must_preserve_resource_feature_declarations() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let profile = Profile::resolve(&root, "m5stack-cores3-gnss").unwrap();
        let expected = profile.catalogs().package("imu").unwrap();
        let original = render(expected, "imu", &profile, "nuttx").unwrap();
        let mut detached = expected.clone();

        // Packaging changes and table order do not alter the construction
        // contract; detached distributions can still adjust their manifests.
        detached.manifest.contents = detached
            .manifest
            .contents
            .replace(
                "[package]",
                "[package]\ndescription = 'Detached distribution'",
            )
            .replace(
                "resource-features = { i2c = \"i2c\", spi = \"spi\" }",
                "resource-features = { spi = \"spi\", i2c = \"i2c\" }",
            );
        assert_eq!(
            render(&detached, "imu", &profile, "nuttx").unwrap(),
            original
        );

        // Both feature names exist, so Cargo can accept the wrong selection.
        // Reject the mismatch before generating a factory for a missing bus.
        detached.manifest.contents = detached
            .manifest
            .contents
            .replace("i2c = \"i2c\"", "i2c = \"spi\"");
        assert!(render(&detached, "imu", &profile, "nuttx")
            .unwrap_err()
            .contains("differ from the resolved profile"));
    }

    #[test]
    fn prepared_handoff_checks_outputs_and_isolates_profile_switches() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap();
        let directory = root.join("platform");
        let features = BTreeSet::from(["imu".into()]);
        let profile = Profile::resolve(&root, "m5stack-cores3-gnss").unwrap();
        let temp = TempDir::new(&std::env::temp_dir()).unwrap();
        let cache = temp.0.join("configuration");
        let out = temp.0.join("cargo");
        let target = "xtensa-esp32s3-nuttx";
        let try_prepare = |profile: &Profile| {
            prepare_consumers(
                [(directory.as_path(), &features)],
                profile,
                target,
                "nuttx",
                &cache,
            )
        };
        let prepare = |profile: &Profile| try_prepare(profile).unwrap().unwrap();
        let apply = |path: &Path, profile: &Profile| {
            consume(path, &directory, "imu", profile, target, "nuttx", &out)
        };
        let handoff = prepare(&profile);
        let stamp = fs::metadata(&handoff).unwrap().modified().unwrap();
        assert_eq!(prepare(&profile), handoff);
        assert_eq!(fs::metadata(&handoff).unwrap().modified().unwrap(), stamp);
        assert!(
            consume(&handoff, &directory, "gnss", &profile, target, "nuttx", &out)
                .unwrap()
                .is_none()
        );
        let inputs = apply(&handoff, &profile).unwrap().unwrap();
        let generated = inputs
            .iter()
            .find(|path| {
                path.starts_with(&cache)
                    && path.extension().is_some_and(|extension| extension == "rs")
            })
            .unwrap();
        let expected = fs::read(out.join("imu_config.rs")).unwrap();
        assert_eq!(expected, fs::read(generated).unwrap());
        assert!(consume(
            &handoff,
            &directory,
            "imu",
            &profile,
            "wrong-nuttx",
            "nuttx",
            &out
        )
        .is_err());
        fs::write(generated, "corrupt generated output").unwrap();
        assert!(apply(&handoff, &profile)
            .unwrap_err()
            .contains("output changed"));
        assert_eq!(fs::read(out.join("imu_config.rs")).unwrap(), expected);
        assert_eq!(prepare(&profile), handoff);
        assert_eq!(fs::read(generated).unwrap(), expected);
        let unbound = Profile::resolve(&root, "esp32s3-camera-stream-mcuboot").unwrap();
        let disabled = prepare(&unbound);
        assert_ne!(disabled, handoff);
        assert!(apply(&handoff, &unbound)
            .unwrap_err()
            .contains("stale prepared"));
        apply(&disabled, &unbound).unwrap();
        assert!(!out.join("imu_config.rs").exists());
        apply(&handoff, &profile).unwrap();
        assert_eq!(fs::read(out.join("imu_config.rs")).unwrap(), expected);
        #[cfg(unix)]
        for location in [&cache, generated.parent().unwrap(), &handoff, generated] {
            let moved = temp.0.join("redirected");
            fs::rename(location, &moved).unwrap();
            std::os::unix::fs::symlink(&moved, location).unwrap();
            assert!(try_prepare(&profile).unwrap_err().contains("symlink"));
            assert!(apply(&handoff, &profile).unwrap_err().contains("symlink"));
            fs::remove_file(location).unwrap();
            fs::rename(moved, location).unwrap();
        }
        fs::write(&handoff, "{}").unwrap();
        assert!(apply(&handoff, &profile)
            .unwrap_err()
            .contains("handoff changed"));
    }

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
