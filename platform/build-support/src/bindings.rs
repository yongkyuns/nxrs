//! Package-owned declarations loaded explicitly from Cargo metadata.
//!
//! Build support has no compile-time dependency on a sibling HAL checkout. A
//! caller chooses package directories; each package names its own data inputs.
use crate::{hash_bytes, read, Result};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Component, Path, PathBuf},
    sync::LazyLock,
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
    pub configuration: BTreeMap<String, Input>,
}

impl Package {
    /// Read only paths declared by this package, relative to its manifest.
    pub fn load(manifest_dir: &Path) -> Result<Self> {
        let root = manifest_dir
            .canonicalize()
            .map_err(|error| format!("package {}: {error}", manifest_dir.display()))?;
        if root.to_str().is_none_or(|value| value.contains(['\r', '\n'])) {
            return Err(format!("invalid package directory {}", root.display()));
        }
        let manifest = input(&root, "Cargo.toml")?;
        let data = parse(&manifest)?;
        let name = data
            .pointer("/package/name")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .ok_or_else(|| format!("{}: package.name must be a string", manifest.path.display()))?
            .to_owned();
        let mut catalogs = Vec::new();
        let mut configuration = BTreeMap::new();
        if let Some(metadata) = data.pointer("/package/metadata/nxrs") {
            let metadata = metadata.as_object().ok_or_else(|| {
                format!("{}: package.metadata.nxrs must be a table", manifest.path.display())
            })?;
            if let Some(paths) = metadata.get("bindings") {
                let paths = paths.as_array().filter(|paths| paths.len() <= 64).ok_or_else(|| {
                    format!("{}: nxrs.bindings must be a list of at most 64 paths", manifest.path.display())
                })?;
                let mut seen = BTreeSet::new();
                for path in paths {
                    let path = path.as_str().ok_or("nxrs.bindings entries must be paths")?;
                    let source = input(&root, path)?;
                    if !seen.insert(source.path.clone()) {
                        return Err(format!("duplicate binding catalog path: {path}"));
                    }
                    catalog(&source)?;
                    catalogs.push(source);
                }
            }
            if let Some(schemas) = metadata.get("configuration") {
                let schemas = schemas.as_object().filter(|schemas| schemas.len() <= 64).ok_or(
                    "nxrs.configuration must be a table of at most 64 provider paths",
                )?;
                for (provider, path) in schemas {
                    if !identifier(provider) {
                        return Err(format!("invalid configuration provider: {provider}"));
                    }
                    let path = path
                        .as_str()
                        .ok_or("nxrs.configuration entries must be paths")?;
                    let source = input(&root, path)?;
                    version(&source, &parse(&source)?)?;
                    configuration.insert(provider.clone(), source);
                }
            }
        }
        Ok(Self {
            name,
            manifest,
            catalogs,
            configuration,
        })
    }

    /// Compare the package name and exact declared catalog/schema content with
    /// a resolved SDK package. Detached consumers may relocate the package and
    /// adjust manifest packaging/dependencies without changing these inputs.
    pub fn same_declarations(&self, expected: &Self) -> bool {
        self.name == expected.name
            && self
                .catalogs
                .iter()
                .map(|input| &input.sha256)
                .eq(expected.catalogs.iter().map(|input| &input.sha256))
            && self
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

    /// All bytes that determine a package's declarations, including its metadata.
    pub fn inputs(&self) -> impl Iterator<Item = &Input> {
        std::iter::once(&self.manifest)
            .chain(self.catalogs.iter())
            .chain(self.configuration.values())
    }
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
            return Err(format!("duplicate binding package for capability: {capability}"));
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

    /// Resolve the unique declaring capability/package and its local feature
    /// requirements. Physical instances do not introduce Cargo feature names.
    pub(crate) fn implementation_features(
        &self,
        compatible: &str,
        bus: &str,
    ) -> Result<(&str, &str, BTreeSet<String>)> {
        let mut matches = self
            .bindings
            .iter()
            .filter(|((_, declared, kind), _)| declared == compatible && kind == bus);
        let Some(((capability, _, _), binding)) = matches.next() else {
            return Err(format!(
                "unsupported compatible/bus binding: {compatible}/{bus}"
            ));
        };
        if let Some(((other, _, _), _)) = matches.next() {
            return Err(format!(
                "ambiguous compatible/bus binding: {compatible}/{bus} is declared by both {capability} and {other}"
            ));
        }
        let package = &self.packages[capability];
        Ok((capability, &package.name, cargo_features(binding)?))
    }

    pub fn package(&self, capability: &str) -> Option<&Package> {
        self.packages.get(capability)
    }

    pub fn packages(&self) -> impl Iterator<Item = (&str, &Package)> {
        self.packages.iter().map(|(capability, package)| (capability.as_str(), package))
    }

    pub fn values<'a>(&'a self, capability: &'a str) -> impl Iterator<Item = &'a Value> + 'a {
        self.bindings
            .iter()
            .filter(move |((owner, _, _), _)| owner.as_str() == capability)
            .map(|(_, value)| value)
    }

    pub fn source_paths(&self) -> impl Iterator<Item = &PathBuf> {
        self.packages.values().flat_map(Package::inputs).map(|input| &input.path)
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
                            "configuration": package.configuration,
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
    let catalog: Catalog = serde_json::from_value(data)
        .map_err(|error| format!("{}: invalid binding catalog: {error}", source.path.display()))?;
    if catalog.version != 1 || catalog.binding.len() > 1024 {
        return Err(format!("{}: invalid binding catalog size or version", source.path.display()));
    }
    for binding in &catalog.binding {
        selector(binding, "compatible", source)?;
        selector(binding, "bus", source)?;
        cargo_features(binding).map_err(|error| format!("{}: {error}", source.path.display()))?;
    }
    Ok(catalog)
}

fn cargo_features(binding: &Value) -> Result<BTreeSet<String>> {
    let Some(features) = binding.get("cargo_features") else {
        return Ok(BTreeSet::new());
    };
    let features = features
        .as_array()
        .filter(|features| features.len() <= 64)
        .ok_or("binding.cargo_features must be a list of at most 64 local Cargo feature names")?;
    // Cargo owns feature existence and resolution. The declaration only admits
    // local names, never dependency forwarding or command-line separators.
    static NAME: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^[\p{XID_Start}_0-9][\p{XID_Continue}+.-]*$")
            .expect("Cargo feature name pattern")
    });
    features
        .iter()
        .map(|feature| {
            feature
                .as_str()
                .filter(|feature| feature.len() <= 128 && NAME.is_match(feature))
                .map(str::to_owned)
                .ok_or_else(|| {
                    format!(
                        "binding.cargo_features entry must be a local Cargo feature name: {feature}"
                    )
                })
        })
        .collect()
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
        .ok_or_else(|| format!("{}: binding.{key} must be a nonempty identifier", source.path.display()))
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'-' | b'_'))
}

fn version(source: &Input, data: &Value) -> Result<()> {
    if data.get("version").and_then(Value::as_u64) != Some(1) {
        return Err(format!("{}: unsupported declaration version (expected 1)", source.path.display()));
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
        || path.components().any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err(format!("package declaration must use a contained relative path: {relative:?}"));
    }
    let path = root.join(path);
    let canonical = path
        .canonicalize()
        .map_err(|error| format!("{}: {error}", path.display()))?;
    if !canonical.starts_with(root) || !canonical.is_file() {
        return Err(format!("package declaration escapes its package or is not a file: {}", path.display()));
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

    const CATALOG: &str = "version = 1\n[[binding]]\ncompatible = 'vendor,sensor'\nbus = 'i2c'\nmodel = 'sensor'\n";
    const SCHEMA: &str = "version = 1\noutput = 'config.rs'\nrole = 'sensor'\n";

    fn fixture(metadata: &str) -> crate::TempDir {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        fs::create_dir(temp.0.join("bindings")).unwrap();
        fs::write(
            temp.0.join("Cargo.toml"),
            format!("[package]\nname = 'nxrs-sensor'\nversion = '0.1.0'\n{metadata}"),
        )
        .unwrap();
        fs::write(temp.0.join("bindings/catalog.toml"), CATALOG).unwrap();
        fs::write(temp.0.join("bindings/config.toml"), SCHEMA).unwrap();
        temp
    }

    fn metadata(path: &str) -> String {
        format!(
            "[package.metadata.nxrs]\nbindings = [{}]\n\
             [package.metadata.nxrs.configuration]\nnuttx = 'bindings/config.toml'\n",
            serde_json::to_string(path).unwrap()
        )
    }

    #[test]
    fn loads_only_declared_package_inputs_and_tracks_exact_bytes() {
        let temp = fixture(&metadata("bindings/catalog.toml"));
        fs::write(temp.0.join("bindings/ignored.toml"), "invalid declaration").unwrap();
        let package = Package::load(&temp.0).unwrap();
        assert_eq!(package.name, "nxrs-sensor");
        assert_eq!(package.inputs().count(), 3);
        assert_eq!(package.catalogs[0].sha256, hash_bytes(CATALOG.as_bytes()));
        let mut catalogs = Catalogs::default();
        catalogs.insert("sensor", package).unwrap();
        assert_eq!(catalogs.binding("sensor", "vendor,sensor", "i2c").unwrap()["model"], "sensor");
        assert!(catalogs.binding("other", "vendor,sensor", "i2c").is_err());
        let before = catalogs.report();
        fs::write(temp.0.join("bindings/config.toml"), format!("{SCHEMA}# changed\n")).unwrap();
        let mut changed = Catalogs::default();
        changed.insert("sensor", Package::load(&temp.0).unwrap()).unwrap();
        assert_ne!(before, changed.report());
        assert_eq!(
            catalogs.package("sensor").unwrap().configuration["nuttx"].contents,
            SCHEMA
        );
    }

    #[test]
    fn a_package_without_declarations_needs_no_sibling_checkout() {
        let temp = fixture("");
        fs::remove_dir_all(temp.0.join("bindings")).unwrap();
        let package = Package::load(&temp.0).unwrap();
        assert_eq!(package.inputs().count(), 1);
        assert!(package.catalogs.is_empty());
        assert!(package.configuration.is_empty());
    }

    #[test]
    fn declaration_equality_allows_packaging_changes_but_preserves_build_inputs() {
        let sdk = fixture(&metadata("bindings/catalog.toml"));
        let expected = Package::load(&sdk.0).unwrap();
        let detached = fixture(&metadata("bindings/catalog.toml"));
        let manifest_path = detached.0.join("Cargo.toml");
        let manifest = fs::read_to_string(&manifest_path).unwrap();
        fs::write(
            &manifest_path,
            format!("{manifest}\n[dev-dependencies]\nqualification = '1.0'\n"),
        )
        .unwrap();
        let actual = Package::load(&detached.0).unwrap();
        assert_ne!(actual.manifest.path, expected.manifest.path);
        assert_ne!(actual.manifest.sha256, expected.manifest.sha256);
        assert!(actual.same_declarations(&expected));

        for (file, contents) in [("catalog.toml", CATALOG), ("config.toml", SCHEMA)] {
            let path = detached.0.join("bindings").join(file);
            fs::write(&path, format!("{contents}# declaration revision\n")).unwrap();
            assert!(!Package::load(&detached.0)
                .unwrap()
                .same_declarations(&expected));
            fs::write(path, contents).unwrap();
        }
        fs::write(&manifest_path, manifest.replace("nuttx =", "linux =")).unwrap();
        assert!(!Package::load(&detached.0)
            .unwrap()
            .same_declarations(&expected));
        fs::write(&manifest_path, manifest.replace("nxrs-sensor", "nxrs-other")).unwrap();
        assert!(!Package::load(&detached.0)
            .unwrap()
            .same_declarations(&expected));
    }

    #[test]
    fn rejects_nonportable_and_escaping_paths() {
        for path in ["../outside.toml", "/outside.toml", "C:/outside.toml", "bindings\\catalog.toml", "./bindings/catalog.toml", "bindings/\nfile.toml", ""] {
            let temp = fixture(&metadata(path));
            let error = Package::load(&temp.0).unwrap_err();
            assert!(error.contains("contained relative path"), "{path:?}: {error}");
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
        for file in ["catalog.toml", "config.toml"] {
            let temp = fixture(&metadata("bindings/catalog.toml"));
            fs::write(temp.0.join("bindings").join(file), "version = 2\n").unwrap();
            assert!(Package::load(&temp.0).unwrap_err().contains("unsupported declaration version"));
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
        let error = catalogs.insert("sensor", Package::load(&temp.0).unwrap()).unwrap_err();
        assert!(error.contains("ambiguous compatible/bus"));
        assert!(catalogs.package("sensor").is_none());
    }

    #[test]
    fn implementation_features_are_local_names_and_form_a_set() {
        let temp = fixture(&metadata("bindings/catalog.toml"));
        fs::write(
            temp.0.join("bindings/catalog.toml"),
            format!(
                "{CATALOG}cargo_features = ['driver-a', 'driver-a', 'driver+b', 'driver.c', '_internal', '2d', '測定']\n"
            ),
        )
        .unwrap();
        let mut catalogs = Catalogs::default();
        catalogs
            .insert("sensor", Package::load(&temp.0).unwrap())
            .unwrap();
        let (capability, package, features) = catalogs
            .implementation_features("vendor,sensor", "i2c")
            .unwrap();
        assert_eq!((capability, package), ("sensor", "nxrs-sensor"));
        assert_eq!(
            features,
            [
                "driver-a",
                "driver+b",
                "driver.c",
                "_internal",
                "2d",
                "測定"
            ]
            .map(str::to_owned)
            .into_iter()
            .collect()
        );
        assert!(
            catalogs
                .implementation_features("vendor,sensor", "spi")
                .unwrap_err()
                .contains("unsupported compatible/bus")
        );
    }

    #[test]
    fn rejects_feature_forwarding_and_malformed_requirements_when_loading_catalogs() {
        for declaration in [
            "'driver'",
            "[42]",
            "['']",
            "['dep:driver']",
            "['alias/driver']",
            "['alias?/driver']",
            "['driver?']",
            "['one,two']",
            "['one two']",
            "['-leading']",
            "['contains!']",
        ] {
            let temp = fixture(&metadata("bindings/catalog.toml"));
            fs::write(
                temp.0.join("bindings/catalog.toml"),
                format!("{CATALOG}cargo_features = {declaration}\n"),
            )
            .unwrap();
            let error = Package::load(&temp.0).unwrap_err();
            assert!(error.contains("cargo_features"), "{declaration}: {error}");
            assert!(error.contains("catalog.toml"), "{error}");
        }
        let oversized = serde_json::json!({"cargo_features": vec!["driver"; 65]});
        assert!(
            cargo_features(&oversized)
                .unwrap_err()
                .contains("at most 64")
        );
        let oversized = serde_json::json!({"cargo_features": ["x".repeat(129)]});
        assert!(cargo_features(&oversized).is_err());
        assert!(cargo_features(&serde_json::json!({})).unwrap().is_empty());
        assert!(
            cargo_features(&serde_json::json!({"cargo_features": []}))
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn implementation_features_require_one_capability_owner() {
        let temp = fixture(&metadata("bindings/catalog.toml"));
        let mut catalogs = Catalogs::default();
        catalogs
            .insert("sensor", Package::load(&temp.0).unwrap())
            .unwrap();
        catalogs
            .insert("other", Package::load(&temp.0).unwrap())
            .unwrap();
        let error = catalogs
            .implementation_features("vendor,sensor", "i2c")
            .unwrap_err();
        assert!(error.contains("ambiguous compatible/bus"), "{error}");
        assert!(
            error.contains("sensor") && error.contains("other"),
            "{error}"
        );
    }
}
