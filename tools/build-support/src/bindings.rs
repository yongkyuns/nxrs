//! Package-owned declarations loaded explicitly from Cargo metadata.
//!
//! Build support has no compile-time dependency on a sibling HAL checkout. A
//! caller chooses package directories; each package names its own data inputs.
use crate::{hash_bytes, read, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Component, Path, PathBuf},
    process::Command,
};

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Input {
    pub path: PathBuf,
    pub sha256: String,
    #[serde(skip)]
    pub contents: String,
}

#[derive(Debug, Clone)]
pub struct Package {
    pub name: String,
    pub consumer_feature: Option<String>,
    pub manifest: Input,
    pub catalogs: Vec<Input>,
    pub dependencies: Vec<Input>,
    pub(crate) drivers: BTreeMap<String, Driver>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Driver {
    pub(crate) dependency: String,
    pub(crate) stores_delay: bool,
    pub(crate) sensor_reference: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct DeviceMetadata {
    #[serde(rename = "capability")]
    _capability: String,
    #[serde(default)]
    stores_delay: bool,
    #[serde(default)]
    sensor_reference: bool,
}

impl Package {
    /// Read only paths declared by this package, relative to its manifest.
    pub fn load(manifest_dir: &Path) -> Result<Self> {
        Self::load_with_dependencies(manifest_dir, None, &mut Vec::new())
    }

    pub(crate) fn load_capability(manifest_dir: &Path, capability: &str) -> Result<Self> {
        Self::load_with_dependencies(manifest_dir, Some(capability), &mut Vec::new())
    }

    fn load_with_dependencies(
        manifest_dir: &Path,
        capability: Option<&str>,
        stack: &mut Vec<PathBuf>,
    ) -> Result<Self> {
        let root = manifest_dir
            .canonicalize()
            .map_err(|error| format!("package {}: {error}", manifest_dir.display()))?;
        if root
            .to_str()
            .is_none_or(|value| value.contains(['\r', '\n']))
        {
            return Err(format!("invalid package directory {}", root.display()));
        }
        if stack.len() >= 64 || stack.contains(&root) {
            return Err(format!(
                "binding dependency cycle or depth limit: {}",
                root.display()
            ));
        }
        stack.push(root.clone());
        let manifest = input(&root, "Cargo.toml")?;
        let data = parse(&manifest)?;
        let name = data
            .pointer("/package/name")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .ok_or_else(|| format!("{}: package.name must be a string", manifest.path.display()))?
            .to_owned();
        let mut catalogs = Vec::new();
        let mut dependencies = Vec::new();
        let mut drivers = BTreeMap::new();
        if let Some(capability) = capability {
            if !device_capabilities(&data)?
                .is_some_and(|capabilities| capabilities.contains(&capability))
            {
                return Err(format!("{name}: missing device capability {capability}"));
            }
            let candidates = data["target"]
                .as_object()
                .into_iter()
                .flatten()
                .filter(|(target, _)| nuttx_target(target))
                .flat_map(|(_, table)| table["dependencies"].as_object().into_iter().flatten())
                .collect::<Vec<_>>();
            for &(alias, declaration) in &candidates {
                // Renamed optional dependencies opt into driver discovery. Cargo
                // supplies the effective alias/name/path for workspace inheritance.
                if declaration["optional"] != true
                    || (declaration.get("package").is_none() && declaration["workspace"] != true)
                {
                    continue;
                }
                let resolved = dependency(&root, &data, alias)?;
                let expected_name = resolved.name;
                dependencies.extend(resolved.inputs);
                let Some(directory) = resolved.directory else {
                    // This can be an unrelated library, not a device driver.
                    // Do not download it merely to inspect its metadata.
                    continue;
                };
                let source = input(&directory, "Cargo.toml")?;
                let driver = parse(&source)?;
                dependencies.push(source);
                let Some(device) = driver
                    .pointer("/package/metadata/nxrs/device")
                    .filter(|device| device["capability"] == capability)
                else {
                    continue;
                };
                let metadata: DeviceMetadata = serde_json::from_value(device.clone())
                    .map_err(|error| format!("driver {alias}: {error}"))?;
                let crate_name = alias.replace('-', "_");
                if !identifier(&crate_name)
                    || matches!(
                        crate_name.as_str(),
                        "crate" | "self" | "Self" | "super" | "_" | "default"
                    )
                    || alias == &expected_name
                    || data["features"].get(alias).is_some()
                    || data["features"].as_object().is_some_and(|features| {
                        let explicit = format!("dep:{alias}");
                        features
                            .values()
                            .filter_map(Value::as_array)
                            .flatten()
                            .any(|value| value == &explicit)
                    })
                {
                    return Err(format!("driver {alias} must expose an implicit Cargo feature and a Rust dependency alias"));
                }
                let collides = |name: &String| {
                    name != alias && name.replace('-', "_").eq_ignore_ascii_case(&crate_name)
                };
                if candidates.iter().any(|(name, _)| collides(name))
                    || ["dependencies", "features"].iter().any(|table| {
                        data[*table]
                            .as_object()
                            .is_some_and(|entries| entries.keys().any(collides))
                    })
                {
                    return Err(format!(
                        "driver alias {alias} collides after Cargo feature normalization"
                    ));
                }
                let imported = Self::load_with_dependencies(&directory, None, stack)?;
                if imported.name != expected_name || imported.catalogs.is_empty() {
                    return Err(format!("binding dependency {alias} must name {expected_name} with declared catalogs"));
                }
                let registration = Driver {
                    dependency: alias.clone(),
                    stores_delay: metadata.stores_delay,
                    sensor_reference: metadata.sensor_reference,
                };
                for input in &imported.catalogs {
                    for binding in catalog(input)?.binding {
                        let compatible = selector(&binding, "compatible", input)?;
                        if drivers
                            .insert(compatible.to_owned(), registration.clone())
                            .is_some_and(|previous| previous != registration)
                        {
                            return Err(format!("conflicting drivers for {compatible}"));
                        }
                    }
                }
                dependencies.extend(imported.dependencies);
                catalogs.extend(imported.catalogs);
            }
        } else if let Some(metadata) = data.pointer("/package/metadata/nxrs") {
            let metadata = metadata.as_object().ok_or_else(|| {
                format!(
                    "{}: package.metadata.nxrs must be a table",
                    manifest.path.display()
                )
            })?;
            if let Some(paths) = metadata.get("bindings") {
                let paths = paths
                    .as_array()
                    .filter(|paths| paths.len() <= 64)
                    .ok_or_else(|| {
                        format!(
                            "{}: expected a binding list with at most 64 entries",
                            manifest.path.display()
                        )
                    })?;
                let mut seen = BTreeSet::new();
                for path in paths {
                    let sources = if let Some(path) = path.as_str() {
                        let source = input(&root, path)?;
                        catalog(&source)?;
                        vec![source]
                    } else {
                        let entry = path
                            .as_object()
                            .filter(|v| v.len() == 1)
                            .ok_or("nxrs.bindings entries must be paths or dependency imports")?;
                        let alias = entry
                            .get("dependency")
                            .and_then(Value::as_str)
                            .ok_or("binding import requires a dependency alias")?;
                        let resolved = dependency(&root, &data, alias)?;
                        let directory = resolved.directory.ok_or_else(|| source_error(alias))?;
                        let expected_name = resolved.name;
                        dependencies.extend(resolved.inputs);
                        let imported = Self::load_with_dependencies(&directory, None, stack)?;
                        if imported.name != expected_name || imported.catalogs.is_empty() {
                            return Err(format!("binding dependency {alias} must name {expected_name} with declared catalogs"));
                        }
                        dependencies.push(imported.manifest);
                        dependencies.extend(imported.dependencies);
                        imported.catalogs
                    };
                    for source in sources {
                        if !seen.insert(source.path.clone()) {
                            return Err(format!(
                                "duplicate binding catalog path: {}",
                                source.path.display()
                            ));
                        }
                        catalogs.push(source);
                    }
                }
            }
            if metadata.contains_key("configuration") {
                return Err("retired nxrs.configuration schema; declare version 2 bindings with implementation identifiers".into());
            }
        }
        stack.pop();
        Ok(Self {
            name,
            consumer_feature: capability.map(str::to_owned),
            manifest,
            catalogs,
            dependencies,
            drivers,
        })
    }

    /// Resource selection is a declaration contract, even when detached packages
    /// adjust unrelated Cargo packaging metadata.
    pub(crate) fn resource_features(&self) -> Result<BTreeMap<String, String>> {
        let manifest = parse(&self.manifest)?;
        let resources = manifest
            .pointer("/package/metadata/nxrs/resource-features")
            .map(|value| {
                serde_json::from_value::<BTreeMap<String, String>>(value.clone())
                    .map_err(|error| format!("{} resource-features: {error}", self.name))
            })
            .transpose()?
            .unwrap_or_default();
        for (kind, feature) in &resources {
            if !["i2c", "spi", "uart", "gpio-interrupt"].contains(&kind.as_str()) {
                return Err(format!(
                    "{} resource-features: unsupported resource kind {kind}",
                    self.name
                ));
            }
            if !manifest["features"][feature].is_array() {
                return Err(format!(
                    "{} resource-features.{kind} must name a declared Cargo feature: {feature}",
                    self.name
                ));
            }
        }
        Ok(resources)
    }

    /// All bytes that determine a package's declarations, including its metadata.
    pub fn inputs(&self) -> impl Iterator<Item = &Input> {
        std::iter::once(&self.manifest)
            .chain(self.dependencies.iter())
            .chain(self.catalogs.iter())
    }
}

fn nuttx_target(selector: &str) -> bool {
    selector
        .trim()
        .strip_prefix("cfg")
        .and_then(|selector| selector.trim_start().strip_prefix('('))
        .and_then(|selector| selector.strip_suffix(')'))
        .and_then(|predicate| predicate.split_once('='))
        .is_some_and(|(name, value)| name.trim() == "target_os" && value.trim() == "\"nuttx\"")
}

struct Dependency {
    directory: Option<PathBuf>,
    name: String,
    inputs: Vec<Input>,
}

/// Imports name existing normal Cargo path dependencies, including inherited and
/// target-specific declarations. Cargo normalizes workspace inheritance; this
/// reader never resolves versions, selects features or downloads package sources.
fn dependency(root: &Path, manifest: &Value, alias: &str) -> Result<Dependency> {
    let tables = std::iter::once(manifest).chain(
        manifest
            .get("target")
            .and_then(Value::as_object)
            .into_iter()
            .flat_map(|v| v.values()),
    );
    let mut selected = BTreeSet::new();
    let mut inherited = false;
    for table in tables {
        if let Some(dep) = table.get("dependencies").and_then(|v| v.get(alias)) {
            if dep["workspace"] == true {
                inherited = true;
                continue;
            }
            let directory = dep
                .get("path")
                .and_then(Value::as_str)
                .map(|path| root.join(path).canonicalize().map_err(|e| e.to_string()))
                .transpose()?;
            let name = dep.get("package").and_then(Value::as_str).unwrap_or(alias);
            selected.insert((directory, name.to_owned()));
        }
    }
    let mut inputs = Vec::new();
    if inherited {
        let metadata = cargo_metadata(&root.join("Cargo.toml"))?;
        let package = metadata["packages"]
            .as_array()
            .ok_or("Cargo metadata is missing packages")?
            .iter()
            .find(|package| {
                package["manifest_path"]
                    .as_str()
                    .and_then(|path| Path::new(path).canonicalize().ok())
                    .is_some_and(|path| path == root.join("Cargo.toml"))
            })
            .ok_or("Cargo metadata is missing the binding package")?;
        for declaration in package["dependencies"]
            .as_array()
            .ok_or("Cargo metadata is missing dependencies")?
            .iter()
            .filter(|dep| {
                dep["kind"].is_null()
                    && dep["rename"].as_str().or(dep["name"].as_str()) == Some(alias)
            })
        {
            let directory = declaration["path"]
                .as_str()
                .map(|path| {
                    Path::new(path)
                        .canonicalize()
                        .map_err(|error| error.to_string())
                })
                .transpose()?;
            let name = declaration["name"]
                .as_str()
                .ok_or("Cargo metadata dependency is missing its package name")?;
            selected.insert((directory, name.to_owned()));
        }
        let workspace = metadata["workspace_root"]
            .as_str()
            .ok_or("Cargo metadata is missing workspace_root")?;
        inputs.push(input(Path::new(workspace), "Cargo.toml")?);
    }
    if selected.len() != 1 {
        return Err(format!(
            "binding dependency {alias} must identify one declared normal Cargo package"
        ));
    }
    let (directory, name) = selected.into_iter().next().unwrap();
    Ok(Dependency {
        directory,
        name,
        inputs,
    })
}

fn source_error(alias: &str) -> String {
    format!("binding dependency {alias} requires a Cargo path dependency (workspace inheritance is supported); Git and registry binding sources are not supported by catalogue discovery")
}

/// Read Cargo's normalized declarations without resolving/fetching dependencies.
/// This is also safe during receipt revalidation in a Cargo build adapter.
fn cargo_metadata(manifest: &Path) -> Result<Value> {
    let output = Command::new(std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into()))
        .current_dir(manifest.parent().ok_or("Cargo manifest has no parent")?)
        .args([
            "metadata",
            "--no-deps",
            "--offline",
            "--format-version",
            "1",
            "--manifest-path",
        ])
        // Cargo 1.90 can reject Windows verbatim canonical manifest paths.
        // The selected parent is already the working directory.
        .arg(
            manifest
                .file_name()
                .ok_or("Cargo manifest has no filename")?,
        )
        .output()
        .map_err(|error| format!("Cargo catalogue discovery failed: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "Cargo catalogue discovery failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("invalid Cargo catalogue metadata: {error}"))
}

pub(crate) fn device_capabilities(manifest: &Value) -> Result<Option<Vec<&str>>> {
    let Some(value) = manifest.pointer("/package/metadata/nxrs/device-capabilities") else {
        return Ok(None);
    };
    let declared = value
        .as_array()
        .ok_or("nxrs.device-capabilities must be a list of Cargo features")?;
    let mut names = Vec::new();
    for capability in declared {
        let name = capability
            .as_str()
            .filter(|name| identifier(name) && manifest["features"][name].is_array())
            .ok_or("device capability must name a declared Cargo feature")?;
        if names.contains(&name) {
            return Err(format!("duplicate device capability {name}"));
        }
        names.push(name);
    }
    Ok(Some(names))
}

#[derive(Debug, Clone, Default)]
pub struct Catalogs {
    pub(crate) discovery_inputs: Vec<Input>,
    packages: BTreeMap<String, Package>,
    bindings: BTreeMap<(String, String, String), Value>,
}

impl Catalogs {
    /// Read catalogue locations only for the capabilities declared by the profile.
    pub(crate) fn load<'a>(
        root: &Path,
        capabilities: impl Iterator<Item = &'a str>,
        product_manifest: Option<&Path>,
    ) -> Result<Self> {
        let workspace_manifest = input(root, "Cargo.toml")?;
        let workspace = parse(&workspace_manifest)?;
        let mut catalogs = Self {
            discovery_inputs: vec![workspace_manifest],
            ..Self::default()
        };
        let sdk_catalogue = catalogue(root, &workspace, &mut catalogs.discovery_inputs)?;
        let product = product_manifest
            .map(|manifest| {
                let metadata = cargo_metadata(manifest)?;
                let workspace_root = metadata["workspace_root"]
                    .as_str()
                    .ok_or("Cargo metadata is missing the product workspace_root")?;
                let workspace_root = Path::new(workspace_root);
                let source = input(workspace_root, "Cargo.toml")?;
                let data = parse(&source)?;
                catalogs.discovery_inputs.push(source);
                catalogs.discovery_inputs.push(input(
                    manifest.parent().ok_or("product manifest has no parent")?,
                    "Cargo.toml",
                )?);
                catalogue(workspace_root, &data, &mut catalogs.discovery_inputs)
            })
            .transpose()?
            .flatten();
        for capability in capabilities {
            let selected = product
                .as_ref()
                .filter(|(_, scopes)| scopes.contains(capability))
                .or_else(|| {
                    sdk_catalogue
                        .as_ref()
                        .filter(|(_, scopes)| scopes.contains(capability))
                });
            let package = match selected {
                Some((directory, _)) => {
                    let mut package = Package::load_capability(directory, capability)?;
                    package.manifest.path = directory.join("Cargo.toml");
                    package
                }
                _ => Package::load(&root.join("hal").join(capability))?,
            };
            catalogs.insert(capability, package)?;
        }
        Ok(catalogs)
    }

    pub fn insert(&mut self, capability: &str, package: Package) -> Result<()> {
        if !identifier(capability) {
            return Err(format!("invalid binding capability: {capability}"));
        }
        if self.packages.contains_key(capability) {
            return Err(format!(
                "duplicate binding package for capability: {capability}"
            ));
        }
        let mut entries = BTreeMap::new();
        for source in &package.catalogs {
            for binding in catalog(source)?.binding {
                let compatible = selector(&binding, "compatible", source)?.to_owned();
                let bus = selector(&binding, "bus", source)?.to_owned();
                let key = (capability.to_owned(), compatible.clone(), bus.clone());
                if entries.insert(key, binding).is_some() {
                    return Err(format!(
                        "{}: ambiguous compatible/bus binding for {capability}: {compatible}/{bus}",
                        source.path.display()
                    ));
                }
            }
        }
        self.bindings.extend(entries);
        self.packages.insert(capability.to_owned(), package);
        Ok(())
    }

    pub fn binding(&self, capability: &str, compatible: &str, bus: &str) -> Result<&Value> {
        self.bindings
            .get(&(capability.to_owned(), compatible.to_owned(), bus.to_owned()))
            .ok_or_else(|| {
                format!(
                    "unsupported compatible/bus binding for {capability}: {compatible}/{bus}; \
                     declare {capability} in [hal] and its package bindings in Cargo metadata; \
                     catalogue discovery currently reads Cargo path dependencies, including workspace inheritance"
                )
            })
    }

    pub fn package(&self, capability: &str) -> Option<&Package> {
        self.packages.get(capability)
    }

    pub fn packages(&self) -> impl Iterator<Item = (&str, &Package)> {
        self.packages
            .iter()
            .map(|(capability, package)| (capability.as_str(), package))
    }

    pub fn values<'a>(&'a self, capability: &'a str) -> impl Iterator<Item = &'a Value> + 'a {
        self.bindings
            .iter()
            .filter(move |((owner, _, _), _)| owner.as_str() == capability)
            .map(|(_, value)| value)
    }

    pub fn source_paths(&self) -> impl Iterator<Item = &PathBuf> {
        self.packages
            .values()
            .flat_map(Package::inputs)
            .chain(self.discovery_inputs.iter())
            .map(|input| &input.path)
    }

    /// Deterministic provenance, intentionally excluding duplicate source bytes.
    pub fn report(&self) -> Value {
        Value::Object(
            self.packages
                .iter()
                .map(|(capability, package)| {
                    (
                        capability.clone(),
                        json!({
                            "package": package.name,
                            "manifest": package.manifest,
                            "bindings": package.catalogs,
                            "dependencies": package.dependencies,
                        }),
                    )
                })
                .collect(),
        )
    }
}

fn catalogue(
    root: &Path,
    workspace: &Value,
    inputs: &mut Vec<Input>,
) -> Result<Option<(PathBuf, BTreeSet<String>)>> {
    workspace
        .pointer("/workspace/metadata/nxrs/device-catalogue")
        .map(|value| {
            let relative = value
                .as_str()
                .ok_or("workspace.metadata.nxrs.device-catalogue must name a package path")?;
            let directory = contained_path(root, relative)?;
            let source = input(&directory, "Cargo.toml")?;
            let manifest = parse(&source)?;
            let scopes = device_capabilities(&manifest)?.ok_or_else(|| {
                format!(
                    "{}: missing package.metadata.nxrs.device-capabilities",
                    directory.display()
                )
            })?;
            let scopes = scopes.into_iter().map(str::to_owned).collect();
            inputs.push(source);
            Ok((directory, scopes))
        })
        .transpose()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Catalog {
    version: u32,
    binding: Vec<Value>,
}

fn catalog(source: &Input) -> Result<Catalog> {
    let data = parse(source)?;
    version(source, &data)?;
    let catalog: Catalog = serde_json::from_value(data).map_err(|error| {
        format!(
            "{}: invalid binding catalog: {error}",
            source.path.display()
        )
    })?;
    if catalog.version != 2 || catalog.binding.len() > 1024 {
        return Err(format!(
            "{}: invalid binding catalog size or version",
            source.path.display()
        ));
    }
    for binding in &catalog.binding {
        selector(binding, "compatible", source)?;
        selector(binding, "bus", source)?;
        let implementation = selector(binding, "implementation", source)?;
        validate_implementation(implementation)?;
    }
    Ok(catalog)
}

/// One checked-in type name, never a Rust path or executable expression.
pub(crate) fn validate_implementation(value: &str) -> Result<()> {
    if value.is_empty()
        || value.len() > 128
        || value == "Self"
        || !value.as_bytes()[0].is_ascii_uppercase()
        || !value.bytes().all(|byte| byte.is_ascii_alphanumeric())
    {
        return Err(format!("invalid implementation identifier: {value:?}"));
    }
    Ok(())
}

fn selector<'a>(binding: &'a Value, key: &str, source: &Input) -> Result<&'a str> {
    binding
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 128
                && value.bytes().all(|byte| byte.is_ascii_graphic())
        })
        .ok_or_else(|| {
            format!(
                "{}: binding.{key} must be a nonempty identifier",
                source.path.display()
            )
        })
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'-' | b'_')
        })
}

fn version(source: &Input, data: &Value) -> Result<()> {
    if data.get("version").and_then(Value::as_u64) != Some(2) {
        return Err(format!(
            "{}: unsupported declaration version (expected 2; migrate model to implementation)",
            source.path.display()
        ));
    }
    Ok(())
}

fn parse(source: &Input) -> Result<Value> {
    let data: toml::Value = toml::from_str(&source.contents)
        .map_err(|error| format!("{}: {error}", source.path.display()))?;
    serde_json::to_value(data).map_err(|error| error.to_string())
}

fn contained_path(root: &Path, relative: &str) -> Result<PathBuf> {
    let path = Path::new(relative);
    if relative.is_empty()
        || relative.contains(['\\', ':', '\0', '\r', '\n'])
        || path.is_absolute()
        || path
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err(format!(
            "package declaration must use a contained relative path: {relative:?}"
        ));
    }
    let path = root.join(path);
    let canonical = path
        .canonicalize()
        .map_err(|error| format!("{}: {error}", path.display()))?;
    if !canonical.starts_with(root) {
        return Err(format!(
            "package declaration escapes its root: {}",
            path.display()
        ));
    }
    Ok(canonical)
}

fn input(root: &Path, relative: &str) -> Result<Input> {
    let canonical = contained_path(root, relative)?;
    let path = root.join(relative);
    if !canonical.is_file() {
        return Err(format!(
            "package declaration is not a file: {}",
            path.display()
        ));
    }
    // Preserve the declared path for Cargo reruns while checking its resolved
    // target above. Moving an internal symlink still invalidates the input.
    let contents = read(&path)?;
    Ok(Input {
        path,
        sha256: hash_bytes(contents.as_bytes()),
        contents,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    const CATALOG: &str = "version = 2\n[[binding]]\ncompatible = 'vendor,sensor'\nbus = 'i2c'\nimplementation = 'Sensor'\n";

    fn fixture(metadata: &str) -> crate::TempDir {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        fs::create_dir(temp.0.join("bindings")).unwrap();
        fs::write(
            temp.0.join("Cargo.toml"),
            format!("[package]\nname = 'nxrs-sensor'\nversion = '0.1.0'\n{metadata}"),
        )
        .unwrap();
        fs::write(temp.0.join("bindings/catalog.toml"), CATALOG).unwrap();
        temp
    }

    fn metadata(path: &str) -> String {
        format!(
            "[package.metadata.nxrs]\nbindings = [{}]\n",
            serde_json::to_string(path).unwrap()
        )
    }

    #[test]
    fn loads_only_declared_package_inputs_and_tracks_exact_bytes() {
        let temp = fixture(&metadata("bindings/catalog.toml"));
        fs::write(temp.0.join("bindings/ignored.toml"), "invalid declaration").unwrap();
        let package = Package::load(&temp.0).unwrap();
        assert_eq!(package.name, "nxrs-sensor");
        assert_eq!(package.inputs().count(), 2);
        assert_eq!(package.catalogs[0].sha256, hash_bytes(CATALOG.as_bytes()));
        let mut catalogs = Catalogs::default();
        catalogs.insert("sensor", package).unwrap();
        assert_eq!(
            catalogs.binding("sensor", "vendor,sensor", "i2c").unwrap()["implementation"],
            "Sensor"
        );
        assert!(catalogs.binding("other", "vendor,sensor", "i2c").is_err());
        let before = catalogs.report();
        fs::write(
            temp.0.join("bindings/catalog.toml"),
            format!("{CATALOG}# changed\n"),
        )
        .unwrap();
        let mut changed = Catalogs::default();
        changed
            .insert("sensor", Package::load(&temp.0).unwrap())
            .unwrap();
        assert_ne!(before, changed.report());
        assert_eq!(
            catalogs.package("sensor").unwrap().catalogs[0].contents,
            CATALOG
        );
    }

    #[test]
    fn a_package_without_declarations_needs_no_sibling_checkout() {
        let temp = fixture("");
        fs::remove_dir_all(temp.0.join("bindings")).unwrap();
        let package = Package::load(&temp.0).unwrap();
        assert_eq!(package.inputs().count(), 1);
        assert!(package.catalogs.is_empty());
    }

    #[test]
    fn rejects_nonportable_and_escaping_paths() {
        for path in [
            "../outside.toml",
            "/outside.toml",
            "C:/outside.toml",
            "bindings\\catalog.toml",
            "./bindings/catalog.toml",
            "bindings/\nfile.toml",
            "",
        ] {
            let temp = fixture(&metadata(path));
            let error = Package::load(&temp.0).unwrap_err();
            assert!(
                error.contains("contained relative path"),
                "{path:?}: {error}"
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlinks_outside_the_declaring_package() {
        let temp = fixture(&metadata("bindings/catalog.toml"));
        let outside = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        fs::write(outside.0.join("catalog.toml"), CATALOG).unwrap();
        fs::remove_file(temp.0.join("bindings/catalog.toml")).unwrap();
        std::os::unix::fs::symlink(
            outside.0.join("catalog.toml"),
            temp.0.join("bindings/catalog.toml"),
        )
        .unwrap();
        assert!(Package::load(&temp.0).unwrap_err().contains("escapes"));
    }

    #[test]
    fn rejects_unknown_versions_and_invalid_metadata() {
        for version in [1, 3] {
            let temp = fixture(&metadata("bindings/catalog.toml"));
            fs::write(
                temp.0.join("bindings/catalog.toml"),
                format!("version = {version}\n"),
            )
            .unwrap();
            assert!(Package::load(&temp.0)
                .unwrap_err()
                .contains("unsupported declaration version"));
        }
        for metadata in [
            "[package.metadata.nxrs]\nbindings = 'bindings/catalog.toml'\n",
            "[package.metadata.nxrs]\nbindings = [42]\n",
            "[package.metadata.nxrs]\nconfiguration = []\n",
            "[package.metadata.nxrs.configuration]\nnuttx = 42\n",
        ] {
            let temp = fixture(metadata);
            assert!(Package::load(&temp.0).is_err());
        }
    }

    #[test]
    fn rejects_ambiguous_matches_across_declared_catalogs() {
        let temp = fixture(
            "[package.metadata.nxrs]\nbindings = ['bindings/catalog.toml', 'bindings/second.toml']\n",
        );
        fs::write(temp.0.join("bindings/second.toml"), CATALOG).unwrap();
        let mut catalogs = Catalogs::default();
        let error = catalogs
            .insert("sensor", Package::load(&temp.0).unwrap())
            .unwrap_err();
        assert!(error.contains("ambiguous compatible/bus"));
        assert!(catalogs.package("sensor").is_none());
    }

    #[test]
    fn driver_catalog_imports_follow_cargo_aliases_and_track_driver_inputs() {
        let temp = fixture(
            "[package.metadata.nxrs]\nbindings=[{dependency='chip'}]\n\
            [target.'cfg(target_os = \"nuttx\")'.dependencies]\n\
            chip={package='nxrs-chip',path='chip'}\n",
        );
        let driver = temp.0.join("chip");
        fs::create_dir(&driver).unwrap();
        fs::write(
            driver.join("Cargo.toml"),
            "[package]\nname='nxrs-chip'\nversion='0.1.0'\n\
            [package.metadata.nxrs]\nbindings=['bindings.toml']\n",
        )
        .unwrap();
        fs::write(driver.join("bindings.toml"), CATALOG).unwrap();
        let package = Package::load(&temp.0).unwrap();
        assert_eq!(package.inputs().count(), 3);
        assert_eq!(
            package.catalogs[0].path,
            driver.join("bindings.toml").canonicalize().unwrap()
        );
        assert_eq!(
            package.dependencies[0].path,
            driver.join("Cargo.toml").canonicalize().unwrap()
        );
        fs::write(
            driver.join("bindings.toml"),
            format!("{CATALOG}# driver edit\n"),
        )
        .unwrap();
        assert_ne!(
            Package::load(&temp.0).unwrap().catalogs[0].sha256,
            package.catalogs[0].sha256
        );

        // A driver cannot escape its own package through a catalog path.
        let manifest = driver.join("Cargo.toml");
        let original = fs::read_to_string(&manifest).unwrap();
        fs::write(
            &manifest,
            original.replace("bindings.toml", "../bindings/catalog.toml"),
        )
        .unwrap();
        assert!(Package::load(&temp.0)
            .unwrap_err()
            .contains("contained relative path"));
        fs::write(
            &manifest,
            original.replace("name='nxrs-chip'", "name='wrong-package'"),
        )
        .unwrap();
        assert!(Package::load(&temp.0)
            .unwrap_err()
            .contains("must name nxrs-chip"));
        fs::write(
            &manifest,
            "[package]\nname='nxrs-chip'\nversion='0.1.0'\n\
            [package.metadata.nxrs]\nbindings=[{dependency='nxrs-sensor'}]\n\
            [dependencies]\nnxrs-sensor={path='..'}\n",
        )
        .unwrap();
        assert!(Package::load(&temp.0).unwrap_err().contains("cycle"));
    }

    #[test]
    fn catalog_imports_cannot_create_undeclared_or_dev_only_dependencies() {
        for dependencies in [
            "",
            "[dev-dependencies]\nchip={path='.'}\n",
            "[dependencies]\nchip='1.0'\n",
        ] {
            let temp = fixture(&format!(
                "[package.metadata.nxrs]\nbindings=[{{dependency='chip'}}]\n{dependencies}"
            ));
            assert!(Package::load(&temp.0)
                .unwrap_err()
                .contains("binding dependency chip"));
        }
    }

    #[test]
    fn driver_discovery_uses_cargo_workspace_inheritance_without_resolving_other_sources() {
        let temp = fixture(
            "[workspace]\n\
             [workspace.dependencies]\nchip={package='nxrs-chip',path='driver'}\n\
             bus-api={package='embedded-hal',version='=1.0.0'}\n\
             [package.metadata.nxrs]\ndevice-capabilities=['imu']\n\
             [features]\nimu=[]\n\
             [dependencies]\nunused={git='https://example.invalid/not-fetched.git',optional=true}\n\
             [target.'cfg(target_os = \"nuttx\")'.dependencies]\nchip={workspace=true,optional=true}\n\
             bus-api={workspace=true,optional=true}\n\
             remote={package='unrelated-library',git='https://example.invalid/not-fetched.git',optional=true}\n",
        );
        fs::create_dir_all(temp.0.join("src")).unwrap();
        fs::write(temp.0.join("src/lib.rs"), "").unwrap();
        let driver = temp.0.join("driver");
        fs::create_dir_all(driver.join("src")).unwrap();
        fs::write(driver.join("src/lib.rs"), "").unwrap();
        fs::write(
            driver.join("Cargo.toml"),
            "[package]\nname='nxrs-chip'\nversion='0.1.0'\n\
             [package.metadata.nxrs]\nbindings=['bindings.toml']\n\
             [package.metadata.nxrs.device]\ncapability='imu'\n",
        )
        .unwrap();
        fs::write(driver.join("bindings.toml"), CATALOG).unwrap();

        let package = Package::load_capability(&temp.0, "imu").unwrap();
        assert_eq!(package.drivers["vendor,sensor"].dependency, "chip");
        assert_eq!(package.catalogs[0].path, driver.join("bindings.toml"));
        assert!(package
            .dependencies
            .iter()
            .any(|input| input.path == temp.0.join("Cargo.toml")));
        assert!(
            !temp.0.join("Cargo.lock").exists(),
            "discovery must not resolve or write a lockfile"
        );

        // Metadata-only discovery still uses the actual inherited package name;
        // the alias is never interpreted as a separate package or source path.
        let manifest = temp.0.join("Cargo.toml");
        let original = fs::read_to_string(&manifest).unwrap();
        fs::write(
            &manifest,
            original.replace("package='nxrs-chip'", "package='wrong-package'"),
        )
        .unwrap();
        assert!(Package::load_capability(&temp.0, "imu").is_err());
    }

    #[test]
    fn non_path_candidates_are_not_fetched_and_explicit_imports_explain_the_limit() {
        let temp = fixture(
            "[package.metadata.nxrs]\ndevice-capabilities=['imu']\n\
             [features]\nimu=[]\n\
             [target.'cfg(target_os = \"nuttx\")'.dependencies]\n\
             chip={package='nxrs-chip',git='https://example.invalid/not-fetched.git',optional=true}\n",
        );
        let package = Package::load_capability(&temp.0, "imu").unwrap();
        assert!(package.catalogs.is_empty());
        let original = fs::read_to_string(temp.0.join("Cargo.toml")).unwrap();
        fs::write(
            temp.0.join("Cargo.toml"),
            original.replace(
                "device-capabilities=['imu']",
                "device-capabilities=['imu']\nbindings=[{dependency='chip'}]",
            ),
        )
        .unwrap();
        let error = Package::load(&temp.0).unwrap_err();
        assert!(
            error.contains("Git and registry binding sources are not supported"),
            "{error}"
        );
        assert!(!temp.0.join("Cargo.lock").exists());
    }
}
