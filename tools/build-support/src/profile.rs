//! One resolver for CLI builds and HAL build scripts.
use crate::{
    bindings::Catalogs,
    composition::{self, Composition},
    hash_bytes, overlay, read, Result,
};
use regex::Regex;
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};
pub const CAPABILITIES: &[&str] = &[
    "console",
    "execution",
    "ssh",
    "detector",
    "camera",
    "audio",
    "settings",
    "storage",
    "led",
    "pulse",
    "imu",
    "gnss",
    "wifi",
    "cellular",
    "ble",
    "tls",
    "boot",
    "watchdog",
    "battery",
    "rtc",
    "flash",
    "display",
    "touch",
];
pub const HOST_OS: &[&str] = &["linux", "macos", "windows"];
const NUTTX_ENDPOINTS: &[&str] =
    &["camera", "audio", "watchdog", "wifi", "rtc", "flash", "display", "touch"];
#[derive(Debug, Clone)]
pub struct Profile {
    pub data: Value,
    pub sources: Vec<PathBuf>,
    inputs: Vec<Input>,
    catalogs: Catalogs,
    sdk_root: Option<PathBuf>,
    origins: overlay::Origins,
    native_config: Option<NativeConfig>,
    native_overlays: Vec<(PathBuf, String)>,
    pub(crate) cli_overlays: Vec<PathBuf>,
    pub(crate) product_manifest: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct NativeConfig {
    pub declared_in: PathBuf,
    pub path: PathBuf,
    pub contents: String,
}

#[derive(Debug, Clone, Serialize)]
struct Input {
    path: PathBuf,
    kind: InputKind,
    sha256: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
enum InputKind {
    Profile,
    DeclaredOverlay,
    CommandLineOverlay,
    BspResources,
    NativeConfig,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Detector {
    pub model: String,
    pub stack_bytes: usize,
    pub sample_ms: u64,
    pub compute_core: Option<u64>,
}

pub fn manifest(path: &Path) -> Result<Value> {
    parse_manifest(path, &read(path)?)
}
fn parse_manifest(path: &Path, input: &str) -> Result<Value> {
    let value: toml::Value =
        toml::from_str(input).map_err(|e| format!("{}: {e}", path.display()))?;
    serde_json::to_value(value).map_err(|e| e.to_string())
}
pub type SourceEdits = BTreeMap<PathBuf, String>;

fn read_source(path: PathBuf, kind: InputKind, edits: &SourceEdits) -> Result<(String, Input)> {
    if path.to_str().is_none_or(|s| s.contains(['\r', '\n'])) {
        return Err(format!("invalid profile input path {}", path.display()));
    }
    let contents = match edits.get(&path) {
        Some(contents) => contents.clone(),
        None => read(&path)?,
    };
    let sha256 = hash_bytes(contents.as_bytes());
    Ok((contents, Input { path, kind, sha256 }))
}

fn read_input(path: PathBuf, kind: InputKind, edits: &SourceEdits) -> Result<(Value, Input)> {
    let (contents, input) = read_source(path, kind, edits)?;
    let data = parse_manifest(&input.path, &contents)?;
    Ok((data, input))
}
fn matches(value: &str, pattern: &str) -> bool {
    Regex::new(pattern).expect("static pattern").is_match(value)
}
fn integer(v: &Value, min: u64, max: u64) -> bool {
    v.as_u64().is_some_and(|n| n >= min && n <= max)
}
fn device(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        matches(s, r"^/dev/[A-Za-z0-9_/.-]{1,120}$") && !s.split('/').any(|p| p == "..")
    })
}
fn keys(v: &Value, required: &[&str], optional: &[&str]) -> bool {
    v.as_object().is_some_and(|m| {
        required.iter().all(|k| m.contains_key(*k))
            && m.keys()
                .all(|k| required.contains(&k.as_str()) || optional.contains(&k.as_str()))
    })
}
pub(crate) fn sensor_stack(v: &Value) -> Result<u64> {
    let stack = v
        .get("stack-bytes")
        .unwrap_or(&Value::Null)
        .as_u64()
        .unwrap_or(16384);
    if v.get("stack-bytes")
        .is_some_and(|x| !integer(x, 3072, 262144))
    {
        return Err("sensor stack-bytes must be between 3072 and 262144".into());
    }
    Ok(stack)
}
#[derive(Default)]
struct ProfileFiles {
    paths: BTreeMap<String, Vec<PathBuf>>,
    defaults: BTreeMap<String, Result<String>>,
}

fn profile_id(name: &str) -> Result<()> {
    if matches!(name, "std" | "native" | "")
        || name.starts_with('-')
        || !name
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
    {
        return Err(format!("unknown or ambiguous platform: {name}"));
    }
    Ok(())
}

fn entries(directory: &Path) -> Result<Vec<PathBuf>> {
    match fs::read_dir(directory) {
        Ok(entries) => entries
            .map(|entry| entry.map(|item| item.path()).map_err(|e| e.to_string()))
            .collect(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(error) => Err(format!("{}: {error}", directory.display())),
    }
}

pub(crate) fn has_profiles(root: &Path) -> bool {
    root.join("tests/profiles").is_dir()
        || entries(&root.join("platform")).is_ok_and(|directories| {
            directories
                .iter()
                .any(|dir| dir.join("profile.toml").is_file())
        })
}

impl ProfileFiles {
    fn read(root: &Path) -> Result<Self> {
        let mut profiles = Self::default();
        for directory in entries(&root.join("platform"))? {
            let default = directory.join("profile.toml");
            if !default.is_file() {
                continue;
            }
            let platform = directory
                .file_name()
                .and_then(|name| name.to_str())
                .ok_or("invalid platform directory name")?;
            let name = (|| {
                profile_id(platform)?;
                let data = manifest(&default)?;
                let name = data["name"].as_str().ok_or_else(|| {
                    format!("{}: default profile requires a name", default.display())
                })?;
                profile_id(name)?;
                Ok(name.to_owned())
            })();
            if let Ok(name) = &name {
                profiles
                    .paths
                    .entry(name.clone())
                    .or_default()
                    .push(default);
            }
            profiles.defaults.insert(platform.to_owned(), name);
            profiles.read_variants(&directory.join("profiles"))?;
        }
        profiles.read_variants(&root.join("tests/profiles"))?;
        Ok(profiles)
    }

    fn read_variants(&mut self, directory: &Path) -> Result<()> {
        for path in entries(directory)? {
            if path.is_file()
                && path
                    .extension()
                    .is_some_and(|extension| extension == "toml")
            {
                let name = path
                    .file_stem()
                    .and_then(|name| name.to_str())
                    .ok_or("invalid profile filename")?
                    .to_owned();
                if profile_id(&name).is_ok() {
                    self.paths.entry(name).or_default().push(path);
                }
            }
        }
        Ok(())
    }

    fn path(&self, name: &str) -> Result<PathBuf> {
        let Some([path]) = self.paths.get(name).map(Vec::as_slice) else {
            return Err(format!("unknown or ambiguous platform: {name}"));
        };
        path.canonicalize().map_err(|error| error.to_string())
    }

    fn default_name(&self, platform: &str) -> Result<String> {
        let name = self
            .defaults
            .get(platform)
            .ok_or_else(|| format!("unknown platform {platform}; run cargo nxrs platforms"))?
            .as_ref()
            .map_err(Clone::clone)?;
        self.path(name)?;
        Ok(name.clone())
    }
}

pub fn profile_path(root: &Path, name: &str) -> Result<PathBuf> {
    ProfileFiles::read(root)?.path(name)
}

/// Discover defaults from each platform directory's profile.toml.
pub fn platform_defaults(root: &Path) -> Result<BTreeMap<String, String>> {
    let profiles = ProfileFiles::read(root)?;
    profiles
        .defaults
        .keys()
        .map(|platform| Ok((platform.clone(), profiles.default_name(platform)?)))
        .collect()
}

/// Select one platform's default independently of other platform files.
pub fn platform_default(root: &Path, platform: &str) -> Result<String> {
    ProfileFiles::read(root)?.default_name(platform)
}

/// Profile inspection accepts existing profile names and platform aliases.
pub fn profile_or_default(root: &Path, name: &str) -> Result<String> {
    profile_id(name)?;
    let profiles = ProfileFiles::read(root)?;
    if !profiles.paths.contains_key(name) && profiles.defaults.contains_key(name) {
        return profiles.default_name(name);
    }
    Ok(name.to_owned())
}
impl Profile {
    /// Construct a profile from existing data for inspection and host fixtures.
    pub fn from_data(data: Value, sources: Vec<PathBuf>) -> Self {
        let origins = sources
            .last()
            .map(|path| (String::new(), path.clone()))
            .into_iter()
            .collect();
        Self {
            data,
            sources,
            inputs: Vec::new(),
            catalogs: Catalogs::default(),
            sdk_root: None,
            origins,
            native_config: None,
            native_overlays: Vec::new(),
            cli_overlays: Vec::new(),
            product_manifest: None,
        }
    }

    /// Construct a fixture with explicitly supplied package declarations.
    pub fn from_data_with_catalogs(data: Value, sources: Vec<PathBuf>, catalogs: Catalogs) -> Self {
        let mut profile = Self::from_data(data, sources);
        profile.catalogs = catalogs;
        profile
    }

    /// Load the declarations of explicitly listed HAL capabilities from an SDK.
    ///
    /// A capability with provider "none" still declares the package whose
    /// unselected physical devices must be validated. No sibling HAL is scanned.
    pub fn load_catalogs(&mut self, sdk_root: &Path) -> Result<()> {
        let root = sdk_root
            .canonicalize()
            .map_err(|error| format!("SDK {}: {error}", sdk_root.display()))?;
        let capabilities = self.data["hal"]
            .as_object()
            .ok_or("[hal] must be a table")?;
        for capability in capabilities.keys() {
            if !CAPABILITIES.contains(&capability.as_str()) {
                return Err(format!("unknown capability: {capability}"));
            }
        }
        self.catalogs = Catalogs::load(
            &root,
            capabilities.keys().map(String::as_str),
            self.product_manifest.as_deref(),
        )?;
        self.sdk_root = Some(root);
        Ok(())
    }

    /// Additive driver and resource features for this capability's physical instances.
    pub fn driver_features(&self, capability: &str) -> Result<BTreeSet<String>> {
        let mut features = BTreeSet::new();
        if self.binding(capability) != "nuttx" {
            return Ok(features);
        }
        if let (Some(graph), Some(package)) =
            (self.composition()?, self.catalogs.package(capability))
        {
            let resources = package.resource_features()?;
            for (_, device) in graph.devices_for(&self.catalogs, capability) {
                if let Some(feature) = package.drivers.get(&device.compatible) {
                    features.insert(feature.dependency.clone());
                }
                let mut used = vec![device.bus.as_str()];
                if let Some(composition::Wake::Interrupt { resource }) = &device.wake {
                    used.push(resource);
                }
                used.extend(device.pps.as_deref());
                for id in used {
                    let resource = graph
                        .resources
                        .get(id)
                        .ok_or_else(|| format!("device references unknown resource {id}"))?;
                    if let Some(feature) = resources
                        .get(&format!("{capability}/{}", resource.kind()))
                        .or_else(|| resources.get(resource.kind()))
                    {
                        features.insert(feature.clone());
                    }
                }
            }
        }
        Ok(features)
    }

    pub fn catalogs(&self) -> &Catalogs {
        &self.catalogs
    }

    pub fn resolve(root: &Path, name: &str) -> Result<Self> {
        Self::resolve_with_overlays(root, name, &[])
    }

    pub fn resolve_with_overlays(root: &Path, name: &str, cli: &[PathBuf]) -> Result<Self> {
        Self::resolve_for_manifest(root, name, cli, None)
    }

    /// Resolve an SDK profile using the selected product's declared catalogue.
    /// Cargo locates that product's workspace; its catalogue replaces only the
    /// capabilities it declares. Other SDK capability declarations remain intact.
    pub fn resolve_for_manifest(
        root: &Path,
        name: &str,
        cli: &[PathBuf],
        product_manifest: Option<&Path>,
    ) -> Result<Self> {
        Self::resolve_with_source_edits(root, name, cli, product_manifest, &SourceEdits::new())
    }

    /// Resolve candidate source bytes through the same inheritance and BSP validation
    /// as a saved profile. Keys are canonical paths or new names under a canonical parent.
    pub fn resolve_with_source_edits(
        root: &Path,
        name: &str,
        cli: &[PathBuf],
        product_manifest: Option<&Path>,
        edits: &SourceEdits,
    ) -> Result<Self> {
        crate::source_transaction::check(root)?;
        let mut declared = Vec::new();
        let profiles = ProfileFiles::read(root)?;
        let mut profile = Self::inherit(root, &profiles, name, &[], &mut declared, edits)?;
        profile.cli_overlays = cli.to_vec();
        profile.product_manifest = product_manifest
            .map(|path| {
                path.canonicalize()
                    .map_err(|error| format!("product manifest {}: {error}", path.display()))
            })
            .transpose()?;
        if declared.len() + cli.len() > 64 {
            return Err("at most 64 overlays can be selected for a profile".into());
        }
        profile.load_native_config(edits)?;
        for (path, kind) in declared
            .into_iter()
            .map(|path| (path, InputKind::DeclaredOverlay))
            .chain(
                cli.iter()
                    .map(|path| (root.join(path), InputKind::CommandLineOverlay)),
            )
        {
            let native = path.extension().is_some_and(|ext| ext == "conf");
            if native && profile.data.get("board").is_none() {
                return Err(format!(
                    "{}: Kconfig overlays require a NuttX board",
                    path.display()
                ));
            }
            if !native && profile.data.get("composition").is_none() {
                return Err("device overlays require a v1 peripheral composition".into());
            }
            let canonical = path
                .canonicalize()
                .map_err(|error| format!("overlay {}: {error}", path.display()))?;
            let input = if native {
                let (contents, input) = read_source(canonical, kind, edits)?;
                profile.native_overlays.push((input.path.clone(), contents));
                input
            } else {
                let (patch, input) = read_input(canonical, kind, edits)?;
                overlay::merge(
                    &mut profile.data,
                    &patch,
                    &input.path,
                    &mut profile.origins,
                    true,
                )?;
                input
            };
            profile.inputs.push(input);
        }
        profile.load_catalogs(root)?;
        profile.resolve_nuttx_resources(root)?;
        profile.validate_resources()?;
        crate::source_transaction::check(root)?;
        for input in &profile.inputs {
            if !edits.contains_key(&input.path) && crate::hash(&input.path)? != input.sha256 {
                return Err(format!("{} changed while resolving the profile; reload", input.path.display()));
            }
        }
        Ok(profile)
    }

    fn load_native_config(&mut self, edits: &SourceEdits) -> Result<()> {
        let Some(reference) = self.data.get("nuttx-config") else {
            return Ok(());
        };
        let declared_in = self.origins.get("nuttx-config").ok_or("missing nuttx-config source")?;
        if self.data.get("board").is_none() {
            return Err(format!("{}: nuttx-config requires a NuttX board", declared_in.display()));
        }
        let relative = reference
            .as_str()
            .filter(|s| !s.is_empty() && !s.contains(['\0', '\r', '\n']))
            .map(Path::new)
            .filter(|path| path.is_relative())
            .ok_or_else(|| format!("{}: nuttx-config must be a relative file path", declared_in.display()))?;
        let path = declared_in.parent().unwrap().join(relative);
        let path = if edits.contains_key(&path) {
            path
        } else {
            path.canonicalize().map_err(|error| format!("{}: {error}", path.display()))?
        };
        let (contents, input) = read_source(path, InputKind::NativeConfig, edits)?;
        self.native_config = Some(NativeConfig {
            declared_in: declared_in.clone(),
            path: input.path.clone(),
            contents,
        });
        self.inputs.push(input);
        Ok(())
    }

    pub fn native_config(&self) -> Option<&NativeConfig> {
        self.native_config.as_ref()
    }

    /// The profile's native config followed by declared and CLI Kconfig overlays.
    pub fn native_config_fragments(&self) -> impl Iterator<Item = (&Path, &str)> {
        self.native_config
            .iter()
            .map(|config| (config.path.as_path(), config.contents.as_str()))
            .chain(
                self.native_overlays
                    .iter()
                    .map(|(path, contents)| (path.as_path(), contents.as_str())),
            )
    }

    pub fn source_of(&self, field: &str) -> Option<&Path> {
        self.origins.get(field).map(PathBuf::as_path)
    }

    pub fn with_source_edits(&self, root: &Path, edits: &SourceEdits) -> Result<Self> {
        Self::resolve_with_source_edits(
            root, self.name(), &self.cli_overlays, self.product_manifest.as_deref(), edits,
        )
    }

    fn resolve_nuttx_resources(&mut self, root: &Path) -> Result<()> {
        if self
            .data
            .get("resources")
            .is_none_or(|v| v.as_object().is_some_and(|o| o.is_empty()))
        {
            return Ok(());
        }
        let board = self.data["board"]
            .as_str()
            .and_then(|s| s.split_once(':'))
            .map(|(board, _)| board)
            .ok_or("physical resources require a NuttX board")?;
        if !matches(board, r"^[a-z0-9][a-z0-9-]*$") {
            return Err("invalid NuttX board resource name".into());
        }
        let path = root
            .join("platform/nuttx/resources")
            .join(format!("{board}.toml"))
            .canonicalize()
            .map_err(|e| format!("BSP resource mapping for {board}: {e}"))?;
        let (_, input) = read_input(path, InputKind::BspResources, &SourceEdits::new())?;
        crate::nuttx_resources::resolve(&mut self.data, &input.path)?;
        self.inputs.push(input);
        Ok(())
    }

    fn inherit(
        root: &Path,
        profiles: &ProfileFiles,
        name: &str,
        chain: &[String],
        declared: &mut Vec<PathBuf>,
        edits: &SourceEdits,
    ) -> Result<Self> {
        if chain.len() >= 16 || chain.iter().any(|s| s == name) {
            return Err(format!("platform inheritance cycle/depth: {name}"));
        }
        let path = profiles.path(name)?;
        let (mut child, input) = read_input(path.clone(), InputKind::Profile, edits)?;
        child.as_object_mut().unwrap().remove("name");
        if child.get("hal-features").is_some() {
            return Err("retired hal-features; platform must own [hal] bindings".into());
        }
        let parent = child.as_object_mut().unwrap().remove("extends");
        let mut resolved = if let Some(parent) = parent {
            let parent = parent.as_str().ok_or("extends must name one platform")?;
            let mut chain = chain.to_vec();
            chain.push(name.into());
            Self::inherit(root, profiles, parent, &chain, declared, edits)?
        } else {
            Self::from_data(json!({"hal":{}}), Vec::new())
        };
        if let Some(overlays) = child.as_object_mut().unwrap().remove("overlays") {
            let names = overlays
                .as_array()
                .ok_or_else(|| format!("{}: overlays must be a list of paths", path.display()))?;
            for entry in names {
                let name = entry
                    .as_str()
                    .filter(|name| !name.is_empty())
                    .ok_or_else(|| {
                        format!("{}: overlay path must be a nonempty string", path.display())
                    })?;
                declared.push(path.parent().unwrap().join(name));
            }
        }
        let bindings = child
            .as_object_mut()
            .unwrap()
            .remove("hal")
            .unwrap_or(json!({}));
        let bindings = bindings.as_object().ok_or("[hal] must be a table")?;
        for (k, v) in bindings {
            if (NUTTX_ENDPOINTS.contains(&k.as_str()) || k == "pulse")
                && resolved.data["hal"][k] == "nuttx"
                && v != "nuttx"
            {
                resolved.data.as_object_mut().unwrap().remove(k);
            }
            resolved.data["hal"][k] = v.clone();
        }
        overlay::merge(
            &mut resolved.data,
            &child,
            &path,
            &mut resolved.origins,
            false,
        )?;
        for (k, v) in child.as_object().unwrap() {
            match k.as_str() {
                "resources" | "devices" | "aliases" => {}
                "remove-resources" | "remove-devices" | "remove-aliases" => {
                    return Err(format!("{}: {k} belongs in an overlay", path.display()));
                }
                _ => {
                    resolved.data[k] = v.clone();
                    if matches!(k.as_str(), "composition" | "imu" | "kconfig" | "nuttx-config") {
                        resolved.origins.insert(k.clone(), path.clone());
                    }
                }
            }
        }
        resolved.data["name"] = json!(name);
        resolved.origins.insert(String::new(), path.clone());
        resolved.sources.push(path);
        resolved.inputs.push(input);
        for (capability, provider) in resolved.data["hal"].as_object().unwrap() {
            if !CAPABILITIES.contains(&capability.as_str()) {
                return Err(format!("unknown capability: {capability}"));
            }
            let provider = provider.as_str().ok_or("invalid HAL binding")?;
            let m = manifest(&root.join("hal").join(capability).join("Cargo.toml"))?;
            let declared = m.pointer("/package/metadata/nxrs/platforms");
            if matches!(provider, "std" | "native")
                || (provider == "simulation" && !["imu", "gnss"].contains(&capability.as_str()))
                || (provider != "none"
                    && provider != "simulation"
                    && !declared
                        .and_then(Value::as_array)
                        .is_some_and(|p| p.iter().any(|x| x == provider)))
            {
                return Err(format!(
                    "{name}: unsupported {capability} implementation: {provider}"
                ));
            }
        }
        resolved.configure_cpus()?;
        Ok(resolved)
    }

    fn profile_source_paths(&self) -> impl Iterator<Item = &PathBuf> {
        self.sources.iter().chain(
            self.inputs
                .iter()
                .filter(|input| input.kind != InputKind::Profile)
                .map(|input| &input.path),
        )
    }

    /// All filesystem inputs for Cargo reruns, receipts, and build identity.
    pub fn source_paths(&self) -> impl Iterator<Item = &PathBuf> {
        self.profile_source_paths()
            .chain(self.catalogs.source_paths())
    }

    /// Ordered profile and overlay provenance for deployment reports.
    pub fn source_hashes(&self) -> Result<Vec<Value>> {
        if self.inputs.is_empty() {
            return self
                .sources
                .iter()
                .map(|path| Ok(json!({"path": path, "sha256": crate::hash(path)?})))
                .collect();
        }
        Ok(self
            .inputs
            .iter()
            .map(|input| json!({"path": input.path, "sha256": input.sha256}))
            .collect())
    }

    pub fn input_fingerprint(&self) -> Option<String> {
        if self.inputs.is_empty() && self.catalogs.source_paths().next().is_none() {
            return None;
        }
        Some(hash_bytes(
            json!({
                "resolver": "peripheral-profile-v1-declarative-inputs",
                "inputs": self.inputs,
                "binding-inputs": self.catalogs.report(),
                "catalogue-discovery": self.catalogs.discovery_inputs,
            })
            .to_string()
            .as_bytes(),
        ))
    }

    pub fn name(&self) -> &str {
        self.data["name"].as_str().unwrap()
    }
    pub fn binding(&self, capability: &str) -> &str {
        self.data["hal"][capability].as_str().unwrap_or("none")
    }
    pub fn composition(&self) -> Result<Option<Composition>> {
        composition::resolve_with_providers_and_origins(
            &self.data,
            self.binding("imu"),
            self.binding("gnss"),
            &self.origins,
            &self.catalogs,
        )
    }
    pub fn json(&self) -> Result<Value> {
        let mut value = self.data.clone();
        let fields = value.as_object_mut().ok_or("profile must be a table")?;
        fields.insert(
            "sources".into(),
            json!(self.profile_source_paths().collect::<Vec<_>>()),
        );
        fields.remove("resolved-composition");
        if let Some(graph) = self.composition()? {
            fields.insert(
                "resolved-composition".into(),
                self.composition_report(&graph),
            );
        }
        Ok(value)
    }
    pub fn composition_report(&self, graph: &Composition) -> Value {
        let binding_sources = self
            .catalogs
            .packages()
            .flat_map(|(_, package)| &package.catalogs)
            .map(|input| {
                self.sdk_root
                    .as_ref()
                    .and_then(|root| input.path.strip_prefix(root).ok())
                    .unwrap_or(&input.path)
                    .to_string_lossy()
                    .replace('\\', "/")
            })
            .collect::<Vec<_>>();
        let semantic = json!({"graph": graph, "target": self.data["target"], "nuttx-resources": self.data.get("resolved-nuttx-resources")});
        let provenance = self
            .origins
            .iter()
            .filter(|(field, _)| !field.is_empty())
            .collect::<std::collections::BTreeMap<_, _>>();
        json!({
            "graph": graph,
            "sources": self.profile_source_paths().collect::<Vec<_>>(),
            "binding-sources": binding_sources,
            "binding-inputs": self.catalogs.report(),
            "source-hashes": self.inputs,
            "provenance": provenance,
            "semantic-digest": hash_bytes(semantic.to_string().as_bytes()),
            "input-fingerprint": self.input_fingerprint(),
        })
    }
    pub fn validate_target(&self, os: &str) -> Result<()> {
        if let Some(value) = self.data.pointer("/build/diagnostics") {
            let enabled = value.as_bool().ok_or("build.diagnostics must be a boolean")?;
            if enabled
                && (os != "nuttx"
                    || self.data.pointer("/build/protected-runtime") == Some(&json!(true)))
            {
                return Err("build.diagnostics requires flat NuttX".into());
            }
        }
        if let Some(list) = self.data.get("target-os") {
            let list = list
                .as_array()
                .ok_or("target-os must be a nonempty array")?;
            if list.is_empty()
                || list.iter().any(|x| {
                    !x.as_str().is_some_and(|s| {
                        HOST_OS.contains(&s) || ["nuttx", "none", "emscripten"].contains(&s)
                    })
                })
            {
                return Err("target-os must be a nonempty array of supported OS names".into());
            }
            if !list.iter().any(|x| x == os) {
                return Err(format!("{} does not support target OS {os}", self.name()));
            }
        }
        let execution = match self.data.get("execution") {
            Some(value) => value.as_str().ok_or("execution must be a string")?,
            None => {
                if self.data.get("board").is_some() {
                    "nuttx"
                } else {
                    "any"
                }
            }
        };
        if !["any", "std", "nuttx", "core", "web"].contains(&execution)
            || (execution == "std" && !HOST_OS.contains(&os))
            || (execution == "nuttx" && os != "nuttx")
            || (execution == "core" && os != "none")
            || (execution == "web" && os != "emscripten")
        {
            return Err(format!(
                "execution {execution} does not support target OS {os}"
            ));
        }
        if self
            .data
            .pointer("/composition/mode")
            .and_then(Value::as_str)
            == Some("simulation")
            && (execution != "std" || !HOST_OS.contains(&os))
        {
            return Err(
                "simulation profiles require std execution on Linux, macOS, or Windows".into(),
            );
        }
        for (name, provider) in self.data["hal"].as_object().unwrap() {
            let p = provider.as_str().unwrap();
            let supported = match p {
                "none" | "mock" => true,
                "simulation" => ["imu", "gnss"].contains(&name.as_str()) && HOST_OS.contains(&os),
                _ => p == os && (HOST_OS.contains(&p) || p == "nuttx"),
            };
            if !supported {
                return Err(format!("{name}/{p} does not support target OS {os}"));
            }
        }
        Ok(())
    }
    pub(crate) fn validate_cargo_target(&self, target: &str, os: &str) -> Result<()> {
        self.validate_target(os)?;
        if let Some(configured) = self.data.get("target") {
            if configured.as_str() != Some(target) {
                return Err(format!(
                    "profile {} declares target {configured}, but Cargo selected {target}",
                    self.name()
                ));
            }
        }
        Ok(())
    }
    pub fn detector(&self) -> Result<Option<Detector>> {
        let Some(v) = self.data.get("detector") else {
            return Ok(None);
        };
        if v.as_object().is_some_and(|o| o.is_empty()) {
            return Ok(None);
        };
        if self.binding("detector") == "none" {
            return Err("detector configuration requires a detector HAL".into());
        }
        if !keys(v, &["model", "stack-bytes", "sample-ms"], &["compute-core"])
            || !["vww", "pico"].iter().any(|s| v["model"] == *s)
            || !integer(&v["stack-bytes"], 1, 0x7fffffff)
            || !integer(&v["sample-ms"], 1, 0x7fffffff)
        {
            return Err("invalid detector product configuration".into());
        }
        let core = v
            .get("compute-core")
            .map(|c| {
                c.as_u64()
                    .filter(|n| *n < 32)
                    .ok_or("invalid detector compute core")
            })
            .transpose()?;
        if core.is_some() && (v["model"] != "pico" || self.binding("detector") != "nuttx") {
            return Err("invalid detector compute core".into());
        }
        Ok(Some(Detector {
            model: v["model"].as_str().unwrap().into(),
            stack_bytes: v["stack-bytes"].as_u64().unwrap() as usize,
            sample_ms: v["sample-ms"].as_u64().unwrap(),
            compute_core: core,
        }))
    }
    pub fn validate_resources(&self) -> Result<()> {
        self.detector()?;
        self.cellular_uart()?;
        if let Some(v) = self.data.get("scheduling") {
            if self.binding("execution") == "none"
                || !keys(v, &["background", "normal"], &[])
                || !integer(&v["background"], 1, 255)
                || !integer(&v["normal"], 1, 255)
                || v["background"].as_u64() >= v["normal"].as_u64()
            {
                return Err(
                    "scheduling requires ordered bounded background and normal priorities".into(),
                );
            }
        } else if self.binding("execution") != "none" {
            return Err(
                "scheduling requires ordered bounded background and normal priorities".into(),
            );
        }
        if let Some(v) = self.data.get("console") {
            if self.binding("console") == "none"
                || !keys(v, &["device"], &[])
                || !device(&v["device"])
            {
                return Err("invalid console device path".into());
            }
        } else if self.binding("console") != "none" {
            return Err("console requires one device".into());
        }
        if let Some(v) = self.data.get("led") {
            if self.binding("led") != "nuttx" || !keys(v, &["device"], &[]) || !device(&v["device"])
            {
                return Err("LED device configuration requires a valid NuttX device path".into());
            }
        }
        let pulse = &self.data["pulse"];
        if self.binding("pulse") == "nuttx" {
            if !keys(pulse, &["pwm-device", "input-device", "stack-bytes"], &[])
                || !device(&pulse["pwm-device"])
                || !device(&pulse["input-device"])
                || pulse["pwm-device"] == pulse["input-device"]
                || !integer(&pulse["stack-bytes"], 8192, 262144)
            {
                return Err(
                    "pulse requires distinct PWM and input device paths and stack-bytes between 8192 and 262144".into(),
                );
            }
        } else if !pulse.is_null() {
            return Err("pulse resource configuration requires its NuttX binding".into());
        }
        self.network_initialization()?;
        for &capability in NUTTX_ENDPOINTS {
            let field = if capability == "wifi" {
                "interface"
            } else {
                "device"
            };
            let value = &self.data[capability];
            if self.binding(capability) == "nuttx" {
                let valid = if capability == "wifi" {
                    value[field].as_str().is_some_and(|v| {
                        !v.is_empty()
                            && v.len() < 16
                            && v.bytes()
                                .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
                    })
                } else {
                    device(&value[field])
                };
                let (required, optional): (&[&str], &[&str]) = match capability {
                    "audio" => (&[field, "slot"], &[]),
                    "watchdog" => (&[field], &["mode"]),
                    _ => (&[field], &[]),
                };
                if !keys(value, required, optional) || !valid {
                    return Err(format!("{capability} requires a valid profile {field}"));
                }
                if capability == "audio"
                    && (value["device"].as_str() != Some("/dev/audio/pcm_in0")
                        || !matches!(value["slot"].as_str(), Some("left" | "right")))
                {
                    return Err("audio requires the I2S0 capture endpoint and a left or right slot".into());
                }
                if capability == "watchdog"
                    && value.get("mode").is_some()
                    && !matches!(value["mode"].as_str(), Some("inherited" | "runtime"))
                {
                    return Err("watchdog mode must be inherited or runtime".into());
                }
            } else if !value.is_null() {
                return Err(format!("{capability} resource requires its NuttX binding"));
            }
        }
        let graph = self.composition()?;
        match (
            self.binding("gnss"),
            self.data.get("gnss"),
            graph.as_ref().and_then(Composition::gnss),
        ) {
            ("nuttx", None, None) => return Err("gnss requires device configuration".into()),
            (provider, Some(_), _) if provider != "nuttx" => {
                return Err("gnss device configuration requires its NuttX binding".into())
            }
            ("nuttx", Some(v), Some(_)) => {
                sensor_stack(v)?;
                if !keys(v, &[], &["stack-bytes"])
                    && !keys(v, &["device", "baud"], &["stack-bytes"])
                {
                    return Err("invalid GNSS UART binding".into());
                }
            }
            ("nuttx", Some(_), None) => {
                return Err("gnss requires a resolved device binding".into())
            }
            _ => {}
        }
        Ok(())
    }
    fn cellular_uart(&self) -> Result<Option<&str>> {
        let value = &self.data["cellular"];
        if self.binding("cellular") != "nuttx" {
            if !value.is_null() {
                return Err("cellular UART requires its NuttX binding".into());
            }
            return Ok(None);
        }
        let uart = value["uart"]
            .as_str()
            .filter(|_| keys(value, &["uart"], &[]))
            .ok_or("cellular requires one named UART resource")?;
        let resource = &self.data["resources"][uart];
        let assigned = self.data["devices"].as_object().is_some_and(|devices| {
            devices.values().any(|device| {
                self.data["resources"][device["bus"].as_str().unwrap_or_default()]["device"]
                    == resource["device"]
            })
        });
        if assigned
            || resource["kind"] != "uart"
            || resource["baud"] != 115200
            || !device(&resource["device"])
        {
            return Err("cellular requires a dedicated 115200-baud BSP UART".into());
        }
        Ok(resource["device"].as_str())
    }

    pub fn watchdog_mode(&self) -> &str {
        self.data["watchdog"]["mode"]
            .as_str()
            .unwrap_or("inherited")
    }

    pub fn network_initialization(&self) -> Result<&str> {
        let Some(init) = self.data.get("init") else {
            return Ok("external");
        };
        let n = init
            .get("network")
            .and_then(Value::as_str)
            .unwrap_or("external");
        if !init.is_object()
            || init.get("network").is_some_and(|v| !v.is_string())
            || !["external", "on-acquire"].contains(&n)
        {
            return Err("invalid network initialization policy".into());
        }
        Ok(n)
    }
    fn configure_cpus(&mut self) -> Result<()> {
        let Some(c) = self.data.get("cpus") else {
            return Ok(());
        };
        if !integer(c, 1, 2) || !self.data["kconfig"].is_object() {
            return Err("cpus requires a NuttX profile and one or two CPUs".into());
        }
        let cpus = c.as_u64().unwrap();
        let config = &mut self.data["kconfig"];
        for key in ["enable", "disable"] {
            let a = config[key].as_array_mut().ok_or("invalid Kconfig table")?;
            a.retain(|v| v != "SMP");
        }
        for key in ["set", "require"] {
            let a = config[key].as_array_mut().ok_or("invalid Kconfig table")?;
            a.retain(|v| {
                !v.as_str().is_some_and(|s| {
                    s.starts_with("SMP_NCPUS=")
                        || s.starts_with("CONFIG_SMP_NCPUS=")
                        || ["CONFIG_SMP=y", "# CONFIG_SMP is not set"].contains(&s)
                })
            });
        }
        config[if cpus == 2 { "enable" } else { "disable" }]
            .as_array_mut()
            .unwrap()
            .push(json!("SMP"));
        config["require"]
            .as_array_mut()
            .unwrap()
            .push(json!(if cpus == 2 {
                "CONFIG_SMP=y"
            } else {
                "# CONFIG_SMP is not set"
            }));
        if cpus == 2 {
            config["set"]
                .as_array_mut()
                .unwrap()
                .push(json!("SMP_NCPUS=2"));
            config["require"]
                .as_array_mut()
                .unwrap()
                .push(json!("CONFIG_SMP_NCPUS=2"));
            for counter in ["ARCH_PERF_EVENTS", "PERF_OVERFLOW_CORRECTION"] {
                config["enable"]
                    .as_array_mut()
                    .unwrap()
                    .retain(|v| v != counter);
                let disabled = config["disable"].as_array_mut().unwrap();
                disabled.retain(|v| v != counter);
                disabled.push(json!(counter));
                config["require"]
                    .as_array_mut()
                    .unwrap()
                    .retain(|v| v != &format!("CONFIG_{counter}=y"));
            }
            let previous = config["forbid-regex"]
                .as_str()
                .ok_or("invalid Kconfig forbid-regex")?;
            // Re-resolving inheritance must not accumulate the derived suffix.
            if !previous.contains("ARCH_PERF_EVENTS|PERF_OVERFLOW_CORRECTION|TIMER_ARCH|ALARM_ARCH")
            {
                config["forbid-regex"] = json!(format!(
                    "(?:{previous})|^CONFIG_(ARCH_PERF_EVENTS|PERF_OVERFLOW_CORRECTION|TIMER_ARCH|ALARM_ARCH)=y$"
                ));
            }
        }
        Ok(())
    }
    pub fn cargo_output(&self, root: &Path, capability: &str, os: &str) -> Result<Vec<String>> {
        if !CAPABILITIES.contains(&capability) {
            return Err(format!("unknown capability {capability}"));
        }
        self.validate_target(os)?;
        let mut output = vec![];
        let mut env =
            |name: &str, value: String| output.push(format!("cargo:rustc-env={name}={value}"));
        match capability {
            "cellular" => {
                if let Some(uart) = self.cellular_uart()? {
                    env("CELLULAR_DEVICE", uart.into());
                }
            }
            "boot" if self.binding(capability) == "nuttx" && self.data.get("mcuboot").is_some() => {
                env("BOOT_UPDATE_CONTROL", "1".into());
            }
            "detector" => {
                if let Some(c) = self.detector()? {
                    if let Some(core) = c.compute_core {
                        env("DETECTOR_COMPUTE_CORE", core.to_string())
                    }
                }
            }
            "wifi" if self.binding(capability) == "nuttx" => {
                env("WIFI_INITIALIZATION", self.network_initialization()?.into());
                env(
                    "WIFI_INTERFACE",
                    self.data["wifi"]["interface"]
                        .as_str()
                        .ok_or("missing wifi interface")?
                        .into(),
                );
            }
            "camera" | "audio" | "watchdog" | "rtc" | "flash" | "display" | "touch"
                if self.binding(capability) == "nuttx" =>
            {
                env(
                    &format!("{}_DEVICE", capability.to_uppercase()),
                    self.data[capability]["device"]
                        .as_str()
                        .ok_or("missing device path")?
                        .into(),
                );
                if capability == "audio" {
                    env(
                        "AUDIO_SLOT",
                        self.data["audio"]["slot"]
                            .as_str()
                            .ok_or("missing audio slot")?
                            .into(),
                    );
                }
                if capability == "watchdog" {
                    env("WATCHDOG_MODE", self.watchdog_mode().into());
                }
            }
            "console" if self.binding(capability) != "none" => env(
                "CONSOLE_DEVICE",
                self.data["console"]["device"].as_str().unwrap().into(),
            ),
            "led" if self.binding(capability) == "nuttx" => env(
                "NXRS_USERLED_PATH",
                self.data["led"]["device"]
                    .as_str()
                    .unwrap_or("/dev/userleds")
                    .into(),
            ),
            "pulse" if self.binding(capability) == "nuttx" => {
                for (field, variable) in [
                    ("pwm-device", "NXRS_PULSE_PWM_DEVICE"),
                    ("input-device", "NXRS_PULSE_INPUT_DEVICE"),
                ] {
                    env(
                        variable,
                        self.data["pulse"][field]
                            .as_str()
                            .ok_or("missing pulse device path")?
                            .into(),
                    );
                }
                env(
                    "NXRS_PULSE_STACK_BYTES",
                    self.data["pulse"]["stack-bytes"].to_string(),
                );
            }
            "execution" if self.binding(capability) != "none" => {
                for key in ["background", "normal"] {
                    env(
                        &format!("EXECUTION_{}_PRIORITY", key.to_uppercase()),
                        self.data["scheduling"][key].to_string(),
                    )
                }
            }
            _ => {}
        }
        output.extend(
            self.source_paths()
                .map(|p| format!("cargo:rerun-if-changed={}", p.display())),
        );
        let mut manifests: BTreeSet<PathBuf> = self.data["hal"]
            .as_object()
            .unwrap()
            .keys()
            .map(|name| root.join("hal").join(name).join("Cargo.toml"))
            .collect();
        manifests.insert(root.join("hal").join(capability).join("Cargo.toml"));
        output.extend(
            manifests
                .iter()
                .map(|p| format!("cargo:rerun-if-changed={}", p.display())),
        );
        let provider = self.binding(capability);
        if provider != "none" {
            output.push(format!("cargo:rustc-cfg=platform=\"{provider}\""));
        }
        Ok(output)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    fn root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap()
    }

    #[test]
    fn native_config_reference_is_relative_to_its_declaring_profile() {
        let temporary = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let root = temporary.0.canonicalize().unwrap();
        fs::write(root.join("Cargo.toml"), "[workspace]\n").unwrap();
        let platform = root.join("platform/board");
        let variants = platform.join("profiles");
        fs::create_dir_all(&variants).unwrap();
        let parent = platform.join("profile.toml");
        let parent_fragment = platform.join("native.conf");
        let child = variants.join("variant.toml");
        let child_fragment = variants.join("native.conf");
        fs::write(&parent, "name='board'\nboard='test:nsh'\nnuttx-config='native.conf'\n[kconfig]\nenable=['DEBUG_FEATURES']\n").unwrap();
        fs::write(&parent_fragment, "CONFIG_DEBUG_ASSERTIONS=y\n").unwrap();
        fs::write(&child, "extends='board'\n").unwrap();
        let inherited = Profile::resolve(&root, "variant").unwrap();
        assert_eq!(inherited.native_config().unwrap().declared_in, parent);
        assert_eq!(inherited.native_config().unwrap().path, parent_fragment);
        assert!(inherited.source_paths().any(|path| path == &parent_fragment));
        fs::write(&parent_fragment, "# CONFIG_DEBUG_ASSERTIONS is not set\n").unwrap();
        let changed = Profile::resolve(&root, "variant").unwrap();
        assert_ne!(changed.input_fingerprint(), inherited.input_fingerprint());

        fs::write(&child, "extends='board'\nnuttx-config='native.conf'\n").unwrap();
        fs::write(&child_fragment, "# child override\n").unwrap();
        let replaced = Profile::resolve(&root, "variant").unwrap();
        assert_eq!(replaced.native_config().unwrap().declared_in, child);
        assert_eq!(replaced.native_config().unwrap().path, child_fragment);
        assert!(!replaced.source_paths().any(|path| path == &parent_fragment));
        assert_eq!(replaced.data["kconfig"], inherited.data["kconfig"]);
    }
    #[test]
    fn stamp_uses_its_own_bsp_uart_at_the_fixed_modem_baud() {
        let root = root();
        for name in ["m5stack-cores3-stamp-catm", "esp32s3-qemu-tracker"] {
            let mut profile = Profile::resolve(&root, name).unwrap();
            assert_eq!(profile.cellular_uart().unwrap(), Some("/dev/ttyS2"));
            assert_eq!(
                profile.data["resources"]["receiver_uart"]["device"],
                "/dev/ttyS1"
            );
            profile.data["resources"]["modem_uart"]["device"] = json!("/dev/ttyS1");
            assert!(profile.cellular_uart().is_err());
            profile.data["resources"]["modem_uart"]["device"] = json!("/dev/ttyS2");
            profile.data["resources"]["modem_uart"]["baud"] = json!(9600);
            assert!(profile.cellular_uart().is_err());
        }
    }

    #[test]
    fn microphone_profile_admits_only_the_bound_endpoint_and_a_valid_slot() {
        let root = root();
        let mut profile = Profile::resolve(&root, "esp32s3-camera-microphone").unwrap();
        let output = profile.cargo_output(&root, "audio", "nuttx").unwrap();
        assert!(output
            .iter()
            .any(|line| line == "cargo:rustc-env=AUDIO_DEVICE=/dev/audio/pcm_in0"));
        assert!(output.iter().any(|line| line == "cargo:rustc-env=AUDIO_SLOT=left"));

        profile.data["audio"]["slot"] = json!("center");
        assert!(profile.validate_resources().is_err());
        profile.data["audio"]["slot"] = json!("right");
        profile.data["audio"]["device"] = json!("/dev/audio/pcm_in1");
        assert!(profile.validate_resources().is_err());
    }

    #[test]
    fn registered_endpoints_are_explicit_validated_profile_inputs() {
        let root = root();
        let base = Profile::resolve(&root, "esp32s3-camera-stream-startup").unwrap();
        for (capability, field, environment, value) in [
            ("camera", "device", "CAMERA_DEVICE", "/dev/video1"),
            ("watchdog", "device", "WATCHDOG_DEVICE", "/dev/watchdog0"),
            ("wifi", "interface", "WIFI_INTERFACE", "wlan1"),
            ("rtc", "device", "RTC_DEVICE", "/dev/rtc0"),
            ("flash", "device", "FLASH_DEVICE", "/dev/mtdblock0"),
            ("display", "device", "DISPLAY_DEVICE", "/dev/spi2"),
            ("touch", "device", "TOUCH_DEVICE", "/dev/input0"),
        ] {
            let mut profile = base.clone();
            profile.data["hal"][capability] = json!("nuttx");
            profile.data[capability][field] = json!(value);
            profile.validate_resources().unwrap();
            assert!(profile
                .cargo_output(&root, capability, "nuttx")
                .unwrap()
                .contains(&format!("cargo:rustc-env={environment}={value}")));
            for invalid in [json!(null), json!(""), json!("../bad\nvalue")] {
                profile.data[capability][field] = invalid;
                assert!(profile.validate_resources().is_err());
            }
        }
        let mut profile = base;
        assert_eq!(profile.watchdog_mode(), "inherited");
        profile.data["watchdog"]["mode"] = json!("runtime");
        profile.validate_resources().unwrap();
        assert!(profile
            .cargo_output(&root, "watchdog", "nuttx")
            .unwrap()
            .contains(&"cargo:rustc-env=WATCHDOG_MODE=runtime".into()));
        profile.data["watchdog"]["mode"] = json!("auto");
        assert!(profile.validate_resources().is_err());
    }

    #[test]
    fn disabling_a_nuttx_provider_drops_its_inherited_endpoint() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let root = &temp.0;
        let profiles = root.join("tests/profiles");
        let wifi = root.join("hal/wifi");
        fs::create_dir_all(&profiles).unwrap();
        fs::create_dir_all(&wifi).unwrap();
        fs::write(root.join("Cargo.toml"), "[workspace]\n").unwrap();
        fs::write(
            wifi.join("Cargo.toml"),
            "[package]\nname='nxrs-wifi'\n[package.metadata.nxrs]\nplatforms=['nuttx']\n",
        )
        .unwrap();
        fs::write(
            profiles.join("base.toml"),
            "[hal]\nwifi='nuttx'\n[wifi]\ninterface='wlan0'\n",
        )
        .unwrap();
        let child = profiles.join("offline.toml");
        let base = "extends='base'\n[hal]\nwifi='none'\n";
        fs::write(&child, base).unwrap();
        let offline = Profile::resolve(root, "offline").unwrap();
        assert_eq!(offline.binding("wifi"), "none");
        assert!(offline.data.get("wifi").is_none());
        fs::write(child, format!("{base}[wifi]\ninterface='wlan1'\n")).unwrap();
        assert!(Profile::resolve(root, "offline").is_err());
    }

    #[test]
    fn profiles_use_actual_os_and_reject_malformed_execution() {
        for (name, os) in [
            ("linux", "linux"),
            ("macos", "macos"),
            ("windows", "windows"),
            ("web", "emscripten"),
            ("esp32s3-radio", "nuttx"),
        ] {
            let mut p = Profile::resolve(&root(), name).unwrap();
            p.validate_target(os).unwrap();
            for bad in [json!(false), json!(42), json!({}), json!("unknown")] {
                p.data["execution"] = bad;
                assert!(p.validate_target(os).is_err());
            }
        }
        for name in ["std", "native", "../linux", "/tmp/linux", ""] {
            assert!(Profile::resolve(&root(), name).is_err());
        }
    }
    #[test]
    fn sensor_and_detector_resources_fail_closed() {
        let base = Profile::resolve(&root(), "m5stack-cores3-gnss").unwrap();
        for (cap, key, bad) in [
            ("gnss", "baud", json!(true)),
            ("gnss", "device", json!("/dev/../ttyS1")),
            ("imu", "address", json!(0x70)),
            ("imu", "frequency", json!(0)),
            ("imu", "irq-device", json!(false)),
            ("gnss", "stack-bytes", json!(3071)),
        ] {
            let mut p = base.clone();
            p.data[cap][key] = bad;
            assert!(p.validate_resources().is_err());
        }
        let base = Profile::resolve(&root(), "esp32s3-camera-stream").unwrap();
        for bad in [json!(-1), json!(32), json!(true)] {
            let mut p = base.clone();
            p.data["detector"]["compute-core"] = bad;
            assert!(p.detector().is_err());
        }
    }
    #[test]
    fn physical_sensor_cargo_output_only_selects_the_provider() {
        let profile = Profile::resolve(&root(), "m5stack-cores3-gnss").unwrap();
        for capability in ["imu", "gnss"] {
            let output = profile.cargo_output(&root(), capability, "nuttx").unwrap();
            assert!(output.contains(&"cargo:rustc-cfg=platform=\"nuttx\"".into()));
            assert!(!output
                .iter()
                .any(|line| line.starts_with("cargo:rustc-env=")));
        }
    }
    #[test]
    fn userled_profile_owns_the_device_binding() {
        let base = Profile::resolve(&root(), "esp32s3-userled").unwrap();
        let output = base.cargo_output(&root(), "led", "nuttx").unwrap();
        assert!(output.contains(&"cargo:rustc-env=NXRS_USERLED_PATH=/dev/nxrs-userled".into()));
        for bad in [
            json!(false),
            json!("/dev/../userleds"),
            json!("/tmp/userleds"),
        ] {
            let mut profile = base.clone();
            profile.data["led"]["device"] = bad;
            assert!(profile.validate_resources().is_err());
        }
        let mut wrong_provider = base;
        wrong_provider.data["hal"]["led"] = json!("mock");
        assert!(wrong_provider.validate_resources().is_err());
    }

    #[test]
    fn pulse_profiles_supply_distinct_endpoints_and_an_explicit_worker_stack() {
        let root = root();
        for (name, pwm, input, stack) in [
            ("esp32s3-peripherals", "/dev/pwm0", "/dev/gpio2", 16384),
            ("pico2w-peripherals", "/dev/pwm7", "/dev/gpio14", 8192),
            ("wroom-pulse", "/dev/pwm0", "/dev/gpio2", 16384),
            ("pico2w-pulse", "/dev/pwm7", "/dev/gpio14", 8192),
        ] {
            let profile = Profile::resolve(&root, name).unwrap();
            assert_eq!(profile.binding("pulse"), "nuttx");
            assert_eq!(
                profile.data["pulse"],
                json!({"pwm-device": pwm, "input-device": input, "stack-bytes": stack}),
            );
            let output = profile.cargo_output(&root, "pulse", "nuttx").unwrap();
            for expected in [
                format!("cargo:rustc-env=NXRS_PULSE_PWM_DEVICE={pwm}"),
                format!("cargo:rustc-env=NXRS_PULSE_INPUT_DEVICE={input}"),
                format!("cargo:rustc-env=NXRS_PULSE_STACK_BYTES={stack}"),
                "cargo:rustc-cfg=platform=\"nuttx\"".into(),
            ] {
                assert!(output.contains(&expected), "{name}: missing {expected}");
            }
        }
        for (name, os) in [("mock", "linux"), ("esp32s3-qemu-pulse-mock", "nuttx")] {
            let profile = Profile::resolve(&root, name).unwrap();
            let output = profile.cargo_output(&root, "pulse", os).unwrap();
            assert!(output.contains(&"cargo:rustc-cfg=platform=\"mock\"".into()));
            assert!(!output
                .iter()
                .any(|line| line.starts_with("cargo:rustc-env=")));
        }
    }

    #[test]
    fn pulse_resources_reject_missing_invalid_and_mismatched_bindings() {
        let base = Profile::resolve(&root(), "pico2w-peripherals").unwrap();
        for field in ["pwm-device", "input-device", "stack-bytes"] {
            let mut profile = base.clone();
            profile.data["pulse"].as_object_mut().unwrap().remove(field);
            assert!(profile.validate_resources().is_err(), "missing {field}");
        }
        let mut missing = base.clone();
        missing.data.as_object_mut().unwrap().remove("pulse");
        assert!(missing.validate_resources().is_err());
        for field in ["pwm-device", "input-device"] {
            for invalid in [
                json!(null),
                json!(false),
                json!(""),
                json!("/tmp/pwm0"),
                json!("/dev/../pwm0"),
                json!("/dev/pwm0\ninvalid"),
            ] {
                let mut profile = base.clone();
                profile.data["pulse"][field] = invalid;
                assert!(profile.validate_resources().is_err(), "invalid {field}");
            }
        }
        for invalid in [
            json!(null),
            json!(true),
            json!(8191),
            json!(262145),
            json!(8192.5),
        ] {
            let mut profile = base.clone();
            profile.data["pulse"]["stack-bytes"] = invalid;
            assert!(profile.validate_resources().is_err());
        }
        let mut same_endpoint = base.clone();
        same_endpoint.data["pulse"]["input-device"] = base.data["pulse"]["pwm-device"].clone();
        assert!(same_endpoint.validate_resources().is_err());
        let mut unexpected = base.clone();
        unexpected.data["pulse"]["frequency"] = json!(25);
        assert!(unexpected.validate_resources().is_err());
        for provider in ["none", "mock", "linux"] {
            let mut profile = base.clone();
            profile.data["hal"]["pulse"] = json!(provider);
            assert!(profile.validate_resources().is_err());
        }
    }

    #[test]
    fn pulse_inheritance_removes_native_inputs_when_the_provider_changes() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let root = &temp.0;
        let profiles = root.join("tests/profiles");
        let pulse = root.join("hal/pulse");
        fs::create_dir_all(&profiles).unwrap();
        fs::create_dir_all(&pulse).unwrap();
        fs::write(root.join("Cargo.toml"), "[workspace]\n").unwrap();
        fs::write(
            pulse.join("Cargo.toml"),
            "[package]\nname='nxrs-pulse'\n[package.metadata.nxrs]\nplatforms=['nuttx','mock']\n",
        )
        .unwrap();
        let resources =
            "[pulse]\npwm-device='/dev/pwm7'\ninput-device='/dev/gpio14'\nstack-bytes=8192\n";
        fs::write(
            profiles.join("base.toml"),
            format!("[hal]\npulse='nuttx'\n{resources}"),
        )
        .unwrap();
        for provider in ["none", "mock"] {
            let contents = format!("extends='base'\n[hal]\npulse='{provider}'\n");
            let child = profiles.join("child.toml");
            fs::write(&child, &contents).unwrap();
            let profile = Profile::resolve(root, "child").unwrap();
            assert_eq!(profile.binding("pulse"), provider);
            assert!(profile.data.get("pulse").is_none());
            fs::write(&child, format!("{contents}{resources}")).unwrap();
            assert!(Profile::resolve(root, "child").is_err());
        }
        fs::write(profiles.join("child.toml"), "[hal]\npulse='linux'\n").unwrap();
        assert!(Profile::resolve(root, "child")
            .unwrap_err()
            .contains("unsupported pulse implementation"));
    }

    #[test]
    fn every_tracked_profile_resolves() {
        let root = root();
        for name in ProfileFiles::read(&root).unwrap().paths.keys() {
            Profile::resolve(&root, name).unwrap();
        }
        for name in platform_defaults(&root).unwrap().values() {
            let default = Profile::resolve(&root, name).unwrap();
            assert!(default
                .sources
                .iter()
                .all(|path| path.starts_with(root.join("platform"))));
        }
    }

    #[test]
    fn simulation_profile_is_host_only_and_emits_only_provider_cfgs() {
        let root = root();
        let profile = Profile::resolve(&root, "simulation").unwrap();
        profile.validate_target("linux").unwrap();
        for os in ["nuttx", "none", "emscripten"] {
            assert!(profile.validate_target(os).is_err(), "{os}");
        }
        let imu = profile.cargo_output(&root, "imu", "linux").unwrap();
        assert!(imu.contains(&"cargo:rustc-cfg=platform=\"simulation\"".into()));
        assert!(!imu.contains(&"cargo:rustc-cfg=hal_enabled".into()));
        assert!(!imu.contains(&"cargo:rustc-cfg=hal_mock".into()));
        let gnss = profile.cargo_output(&root, "gnss", "linux").unwrap();
        assert!(gnss.contains(&"cargo:rustc-cfg=platform=\"simulation\"".into()));
        let absent = profile.cargo_output(&root, "camera", "linux").unwrap();
        assert!(!absent
            .iter()
            .any(|line| line.starts_with("cargo:rustc-cfg=")));
    }

    #[test]
    fn simulation_config_is_fixed_and_uses_typed_sources() {
        let root = root();
        let profile = Profile::resolve(&root, "simulation").unwrap();
        let temp =
            std::env::temp_dir().join(format!("nxrs-simulation-config-{}", std::process::id()));
        fs::create_dir_all(&temp).unwrap();
        crate::write_simulation_config(&profile, &temp).unwrap();
        assert_eq!(
            fs::read_to_string(temp.join("simulation_config.rs")).unwrap(),
            "const SIM_SEED: u64 = 274;\nconst SIM_SOURCES: &[simulation::Source] = &[simulation::Source::Imu, simulation::Source::Gnss];\n"
        );
        fs::remove_dir_all(temp).unwrap();
    }
    #[test]
    fn explicit_sdk_catalogs_track_catalog_bytes_without_scanning_other_hals() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let workspace = temp.0.canonicalize().unwrap().join("Cargo.toml");
        let declarations = "[workspace.metadata.nxrs]\ndevice-catalogue='wiring/sensor'\n";
        fs::write(&workspace, declarations).unwrap();
        let directory = temp.0.join("wiring/sensor");
        fs::create_dir_all(&directory).unwrap();
        let driver = temp.0.join("driver");
        let unrelated = temp.0.join("other");
        fs::create_dir(&driver).unwrap();
        fs::create_dir(&unrelated).unwrap();
        fs::write(
            unrelated.join("Cargo.toml"),
            "[package]\nname='other-driver'\n[package.metadata.nxrs.device]\ncapability='gnss'\nstores-delay='invalid'\n",
        ).unwrap();
        fs::write(
            directory.join("Cargo.toml"),
            "[package]\nname='nxrs-composition'\nversion='0.1.0'\n\
             [features]\nimu=[]\ngnss=[]\n[package.metadata.nxrs]\ndevice-capabilities=['imu','gnss']\n\
             [target.'cfg(target_os = \"nuttx\")'.dependencies]\nfixture={package='fixture-driver',path='../../driver',optional=true}\nother={package='other-driver',path='../../other',optional=true}\n",
        )
        .unwrap();
        fs::write(
            driver.join("Cargo.toml"),
            "[package]\nname='fixture-driver'\n[package.metadata.nxrs]\nbindings=['bindings.toml']\n\
             [package.metadata.nxrs.device]\ncapability='imu'\n",
        )
        .unwrap();
        let catalog = "version=2\n[[binding]]\ncompatible='vendor,sensor'\nbus='i2c'\nimplementation='Sensor'\n";
        fs::write(driver.join("bindings.toml"), catalog).unwrap();
        let profile_path = temp.0.join("profile.toml");
        let profile_contents = "[hal]\nimu='none'\n";
        fs::write(&profile_path, profile_contents).unwrap();
        let mut profile =
            Profile::from_data(json!({"hal":{"imu":"none"}}), vec![profile_path.clone()]);
        assert!(profile.catalogs().package("imu").is_none());
        profile.load_catalogs(&temp.0).unwrap();
        assert_eq!(
            profile
                .catalogs()
                .source_paths()
                .collect::<BTreeSet<_>>()
                .len(),
            5
        );
        assert!(profile.catalogs().package("gnss").is_none());
        assert_eq!(profile.source_paths().collect::<BTreeSet<_>>().len(), 6);
        assert!(profile.source_paths().any(|path| path == &workspace));
        assert!(profile
            .source_paths()
            .any(|path| path.ends_with("bindings.toml")));
        assert_eq!(profile.json().unwrap()["sources"], json!([profile_path]));
        assert_eq!(
            profile.source_hashes().unwrap(),
            vec![json!({
                "path": profile_path,
                "sha256": hash_bytes(profile_contents.as_bytes()),
            })]
        );
        let initial = profile.input_fingerprint().unwrap();

        fs::write(
            driver.join("bindings.toml"),
            format!("{catalog}# first edit\n"),
        )
        .unwrap();
        let mut changed = profile.clone();
        changed.load_catalogs(&temp.0).unwrap();
        assert_ne!(changed.input_fingerprint().unwrap(), initial);
        assert_eq!(
            profile.catalogs().package("imu").unwrap().catalogs[0].contents,
            catalog
        );
        let first_fingerprint = changed.input_fingerprint().unwrap();
        fs::write(
            driver.join("bindings.toml"),
            format!("{catalog}# catalog edit\n"),
        )
        .unwrap();
        changed.load_catalogs(&temp.0).unwrap();
        assert_ne!(changed.input_fingerprint().unwrap(), first_fingerprint);
        assert_eq!(
            changed.catalogs().report()["imu"]["bindings"][0]["sha256"],
            hash_bytes(format!("{catalog}# catalog edit\n").as_bytes())
        );
        assert_eq!(
            changed.source_hashes().unwrap(),
            profile.source_hashes().unwrap()
        );
        assert_eq!(changed.json().unwrap()["sources"], json!([profile_path]));

        changed.data["hal"]["gnss"] = json!("none");
        assert!(changed
            .load_catalogs(&temp.0)
            .unwrap_err()
            .contains("driver other"));
        fs::write(
            &workspace,
            declarations.replace("wiring/sensor", "../sensor"),
        )
        .unwrap();
        let error = profile.load_catalogs(&temp.0).unwrap_err();
        assert!(error.contains("contained relative path"));
    }

    #[test]
    fn only_the_pps_profile_enables_the_gnss_gpio_feature() {
        let uart = Profile::resolve(&root(), "m5stack-cores3-gnss").unwrap();
        assert_eq!(uart.driver_features("gnss").unwrap(), BTreeSet::from(["ubx".into()]));

        let pps = Profile::resolve(&root(), "m5stack-cores3-dual-imu").unwrap();
        assert_eq!(
            pps.driver_features("gnss").unwrap(),
            BTreeSet::from(["gnss-pps".into(), "ubx".into()])
        );
        assert!(!pps.driver_features("imu").unwrap().contains("gnss-pps"));
        let generated = crate::configuration::render_instances(
            &pps.composition().unwrap().unwrap(),
            pps.catalogs(),
            "gnss",
            16_384,
        )
        .unwrap();
        assert!(generated.contains("pps = \"/dev/gpio10\""));
    }

    #[test]
    fn resource_feature_mappings_use_declared_package_features() {
        let profile = Profile::resolve(&root(), "m5stack-cores3-gnss").unwrap();
        let with_manifest = |replacements: &[(&str, &str)]| {
            let mut catalogs = Catalogs::default();
            for (capability, package) in profile.catalogs().packages() {
                let mut package = package.clone();
                for (from, to) in replacements {
                    package.manifest.contents = package.manifest.contents.replace(from, to);
                }
                catalogs.insert(capability, package).unwrap();
            }
            Profile::from_data_with_catalogs(profile.data.clone(), vec![], catalogs)
        };
        let renamed = with_manifest(&[
            ("i2c = \"i2c\"", "i2c = \"sensor-i2c\""),
            ("[features]", "[features]\nsensor-i2c = [\"i2c\"]"),
        ]);
        assert_eq!(
            renamed.driver_features("imu").unwrap(),
            BTreeSet::from(["bmi270".into(), "sensor-i2c".into()])
        );
        assert_eq!(
            renamed.driver_features("gnss").unwrap(),
            BTreeSet::from(["ubx".into()])
        );
        for (from, to, expected) in [
            (
                "i2c = \"i2c\"",
                "i22c = \"i2c\"",
                "unsupported resource kind",
            ),
            (
                "i2c = \"i2c\"",
                "i2c = \"missing\"",
                "declared Cargo feature",
            ),
            ("i2c = \"i2c\"", "i2c = 1", "resource-features"),
        ] {
            let error = with_manifest(&[(from, to)])
                .driver_features("imu")
                .unwrap_err();
            assert!(error.contains(expected), "{error}");
        }
    }
}
