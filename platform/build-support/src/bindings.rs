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
    pub manifest: Input,
    pub catalogs: Vec<Input>,
    pub dependencies: Vec<Input>,
}

impl Package {
    /// Read only paths declared by this package, relative to its manifest.
    pub fn load(manifest_dir: &Path) -> Result<Self> {
        Self::load_with_dependencies(manifest_dir, &mut Vec::new())
    }

    fn load_with_dependencies(manifest_dir: &Path, stack: &mut Vec<PathBuf>) -> Result<Self> {
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
        if let Some(metadata) = data.pointer("/package/metadata/nxrs") {
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
                            "{}: nxrs.bindings must be a list of at most 64 paths",
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
                        let (directory, expected_name) = dependency(&root, &data, alias)?;
                        let imported = Self::load_with_dependencies(&directory, stack)?;
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
            manifest,
            catalogs,
            dependencies,
        })
    }

    /// All bytes that determine a package's declarations, including its metadata.
    pub fn inputs(&self) -> impl Iterator<Item = &Input> {
        std::iter::once(&self.manifest)
            .chain(self.dependencies.iter())
            .chain(self.catalogs.iter())
    }
}

/// Imports name existing normal Cargo path dependencies, including target tables.
/// They do not select features or introduce another package-resolution mechanism.
fn dependency(root: &Path, manifest: &Value, alias: &str) -> Result<(PathBuf, String)> {
    let tables = std::iter::once(manifest).chain(
        manifest
            .get("target")
            .and_then(Value::as_object)
            .into_iter()
            .flat_map(|v| v.values()),
    );
    let mut selected = BTreeSet::new();
    for table in tables {
        if let Some(dep) = table.get("dependencies").and_then(|v| v.get(alias)) {
            let path = dep.get("path").and_then(Value::as_str).ok_or_else(|| {
                format!("binding dependency {alias} requires an explicit Cargo path")
            })?;
            let directory = root.join(path).canonicalize().map_err(|e| e.to_string())?;
            let name = dep.get("package").and_then(Value::as_str).unwrap_or(alias);
            selected.insert((directory, name.to_owned()));
        }
    }
    if selected.len() != 1 {
        return Err(format!(
            "binding dependency {alias} must identify one declared normal Cargo package"
        ));
    }
    Ok(selected.into_iter().next().unwrap())
}

#[derive(Debug, Clone, Default)]
pub struct Catalogs {
    packages: BTreeMap<String, Package>,
    bindings: BTreeMap<(String, String, String), Value>,
}

impl Catalogs {
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
                     declare {capability} in [hal] and its package bindings in Cargo metadata"
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

fn input(root: &Path, relative: &str) -> Result<Input> {
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
    if !canonical.starts_with(root) || !canonical.is_file() {
        return Err(format!(
            "package declaration escapes its package or is not a file: {}",
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
}
