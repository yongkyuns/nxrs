//! One resolver for CLI builds and HAL build scripts.
use crate::{
    composition::{self, Composition},
    hash_bytes, overlay, read, Result,
};
use regex::Regex;
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
};
pub const CAPABILITIES: &[&str] = &[
    "console",
    "execution",
    "detector",
    "camera",
    "config",
    "storage",
    "transport",
    "led",
    "imu",
    "gnss",
    "wifi",
    "ble",
    "tls",
    "boot",
    "watchdog",
];
pub const HOST_OS: &[&str] = &["linux", "macos", "windows"];
#[derive(Debug, Clone)]
pub struct Profile {
    pub data: Value,
    pub sources: Vec<PathBuf>,
    inputs: Vec<Input>,
    origins: overlay::Origins,
    pub(crate) cli_overlays: Vec<PathBuf>,
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
fn read_input(path: PathBuf, kind: InputKind) -> Result<(Value, Input)> {
    if path.to_str().is_none_or(|s| s.contains(['\r', '\n'])) {
        return Err(format!("invalid profile input path {}", path.display()));
    }
    let contents = read(&path)?;
    let data = parse_manifest(&path, &contents)?;
    let sha256 = hash_bytes(contents.as_bytes());
    Ok((data, Input { path, kind, sha256 }))
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
        .is_some_and(|x| !integer(x, 8192, 262144))
    {
        return Err("sensor stack-bytes must be between 8192 and 262144".into());
    }
    Ok(stack)
}
pub fn profile_path(root: &Path, name: &str) -> Result<PathBuf> {
    if matches!(name, "std" | "native") || !matches(name, r"^[a-z0-9][a-z0-9-]*$") {
        return Err(format!("unknown or ambiguous platform: {name}"));
    }
    let paths: Vec<_> = ["platform/profiles", "platform/nuttx/platforms"]
        .into_iter()
        .map(|p| root.join(p).join(format!("{name}.toml")))
        .filter(|p| p.is_file())
        .collect();
    if paths.len() != 1 {
        return Err(format!("unknown or ambiguous platform: {name}"));
    }
    paths[0].canonicalize().map_err(|e| e.to_string())
}
impl Profile {
    /// Construct a profile from existing data for inspection and host fixtures.
    pub fn from_data(data: Value, sources: Vec<PathBuf>) -> Self {
        let origins = sources
            .last()
            .map(|path| (String::new(), path.clone()))
            .into_iter()
            .collect();
        let mut profile = Self {
            data,
            sources,
            inputs: Vec::new(),
            origins,
            cli_overlays: Vec::new(),
        };
        profile.resolve_native_endpoints();
        profile
    }

    pub fn resolve(root: &Path, name: &str) -> Result<Self> {
        Self::resolve_with_overlays(root, name, &[])
    }

    pub fn resolve_with_overlays(root: &Path, name: &str, cli: &[PathBuf]) -> Result<Self> {
        let mut declared = Vec::new();
        let mut profile = Self::inherit(root, name, &[], &mut declared)?;
        profile.cli_overlays = cli.to_vec();
        if declared.len() + cli.len() > 64 {
            return Err("at most 64 overlays can be selected for a profile".into());
        }
        let has_composition = profile.data.get("composition").is_some();
        if !has_composition && (!declared.is_empty() || !cli.is_empty()) {
            // An endpoint overlay cannot migrate legacy sensor bindings.
            profile.composition()?;
        }
        for (path, kind) in declared
            .into_iter()
            .map(|path| (path, InputKind::DeclaredOverlay))
            .chain(
                cli.iter()
                    .map(|path| (root.join(path), InputKind::CommandLineOverlay)),
            )
        {
            let canonical = path
                .canonicalize()
                .map_err(|error| format!("overlay {}: {error}", path.display()))?;
            let (patch, input) = read_input(canonical, kind)?;
            if !has_composition
                && !patch.as_object().is_some_and(|fields| {
                    !fields.is_empty()
                        && fields
                            .keys()
                            .all(|key| overlay::NATIVE_ENDPOINT_TABLES.contains(&key.as_str()))
                })
            {
                return Err(
                    "without a v1 peripheral composition, overlays may only set camera or BLE endpoints"
                        .into(),
                );
            }
            overlay::merge(
                &mut profile.data,
                &patch,
                &input.path,
                &mut profile.origins,
                true,
            )?;
            profile.inputs.push(input);
        }
        profile.resolve_native_endpoints();
        profile.validate_resources()?;
        Ok(profile)
    }

    fn resolve_native_endpoints(&mut self) {
        for (capability, key, value) in [
            ("camera", "device", "/dev/video0"),
            ("ble", "scan-interface", "bnep0"),
        ] {
            if self.binding(capability) == "nuttx" && self.data.get(capability).is_none() {
                self.data[capability][key] = json!(value);
            }
        }
    }

    fn inherit(
        root: &Path,
        name: &str,
        chain: &[String],
        declared: &mut Vec<PathBuf>,
    ) -> Result<Self> {
        if chain.len() >= 16 || chain.iter().any(|s| s == name) {
            return Err(format!("platform inheritance cycle/depth: {name}"));
        }
        let path = profile_path(root, name)?;
        let (mut child, input) = read_input(path.clone(), InputKind::Profile)?;
        if child.get("hal-features").is_some() {
            return Err("retired hal-features; platform must own [hal] bindings".into());
        }
        let parent = child.as_object_mut().unwrap().remove("extends");
        let mut resolved = if let Some(parent) = parent {
            let parent = parent.as_str().ok_or("extends must name one platform")?;
            let mut chain = chain.to_vec();
            chain.push(name.into());
            Self::inherit(root, parent, &chain, declared)?
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
                "resources" | "devices" | "roles" => {}
                key if overlay::NATIVE_ENDPOINT_TABLES.contains(&key) => {}
                "remove-resources" | "remove-devices" | "remove-roles" => {
                    return Err(format!("{}: {k} belongs in an overlay", path.display()));
                }
                _ => {
                    resolved.data[k] = v.clone();
                    if matches!(k.as_str(), "composition" | "imu") {
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

    pub fn source_paths(&self) -> impl Iterator<Item = &PathBuf> {
        self.sources.iter().chain(
            self.inputs
                .iter()
                .filter(|input| input.kind != InputKind::Profile)
                .map(|input| &input.path),
        )
    }

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
        if self.inputs.is_empty() {
            return None;
        }
        let binding = (self.binding("imu") == "nuttx").then(composition::binding_schema_digest);
        let gnss_binding =
            (self.binding("gnss") == "nuttx").then(composition::gnss_binding_schema_digest);
        Some(hash_bytes(
            json!({
                "resolver": "peripheral-profile-v2",
                "inputs": self.inputs,
                "binding": binding,
                "gnss-binding": gnss_binding,
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
        )
    }
    pub fn json(&self) -> Result<Value> {
        let mut value = self.data.clone();
        let fields = value.as_object_mut().ok_or("profile must be a table")?;
        fields.insert(
            "sources".into(),
            json!(self.source_paths().collect::<Vec<_>>()),
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
        let binding_sources = graph
            .imu()
            .map(|_| composition::BINDING_SCHEMA_PATH)
            .into_iter()
            .chain(graph.gnss().map(|_| composition::GNSS_BINDING_SCHEMA_PATH))
            .collect::<Vec<_>>();
        let semantic = json!({"graph": graph, "target": self.data["target"]});
        let provenance = self
            .origins
            .iter()
            .filter(|(field, _)| !field.is_empty())
            .collect::<std::collections::BTreeMap<_, _>>();
        json!({
            "graph": graph,
            "sources": self.source_paths().collect::<Vec<_>>(),
            "binding-sources": binding_sources,
            "source-hashes": self.inputs,
            "provenance": provenance,
            "semantic-digest": hash_bytes(semantic.to_string().as_bytes()),
            "input-fingerprint": self.input_fingerprint(),
        })
    }
    pub fn validate_target(&self, os: &str) -> Result<()> {
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
        if let Some(v) = self.data.get("camera") {
            if self.binding("camera") != "nuttx"
                || !keys(v, &["device"], &[])
                || !device(&v["device"])
            {
                return Err("camera.device requires a valid NuttX device path".into());
            }
        }
        if let Some(v) = self.data.get("ble") {
            if self.binding("ble") != "nuttx"
                || !keys(v, &["scan-interface"], &[])
                || !v["scan-interface"]
                    .as_str()
                    .is_some_and(|name| matches(name, r"^[A-Za-z0-9][A-Za-z0-9_-]{0,14}$"))
            {
                return Err("ble.scan-interface requires a valid NuttX interface name".into());
            }
        }
        self.network_initialization()?;
        self.composition()?;
        if self.binding("gnss") != "nuttx" && self.data.get("gnss").is_some() {
            return Err("gnss device configuration requires its NuttX binding".into());
        }
        Ok(())
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
            "detector" => {
                if let Some(c) = self.detector()? {
                    if let Some(core) = c.compute_core {
                        env("DETECTOR_COMPUTE_CORE", core.to_string())
                    }
                }
            }
            "wifi" if self.binding(capability) == "nuttx" => {
                env("WIFI_INITIALIZATION", self.network_initialization()?.into())
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
            "camera" if self.binding(capability) == "nuttx" => env(
                "CAMERA_DEVICE",
                self.data["camera"]["device"]
                    .as_str()
                    .ok_or("camera.device is missing from the resolved NuttX profile")?
                    .into(),
            ),
            "ble" if self.binding(capability) == "nuttx" => env(
                "BLE_SCAN_INTERFACE",
                self.data["ble"]["scan-interface"]
                    .as_str()
                    .ok_or("ble.scan-interface is missing from the resolved NuttX profile")?
                    .into(),
            ),
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
        if self.binding("imu") == "nuttx" {
            output.push(format!(
                "cargo:rerun-if-changed={}",
                root.join(composition::BINDING_SCHEMA_PATH).display()
            ));
        }
        if self.binding("gnss") == "nuttx" {
            output.push(format!(
                "cargo:rerun-if-changed={}",
                root.join(composition::GNSS_BINDING_SCHEMA_PATH).display()
            ));
        }
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
        let provider = match self.binding(capability) {
            "none" => None,
            mode @ ("mock" | "simulation") => Some(mode),
            _ => Some("hardware"),
        };
        if let Some(provider) = provider {
            output.push(format!("cargo:rustc-cfg=hal_provider=\"{provider}\""));
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
            ("gnss", "stack-bytes", json!(1)),
        ] {
            let mut p = base.clone();
            p.data[cap][key] = bad;
            assert!(p.validate_resources().is_err());
        }
        let base = Profile::resolve(&root(), "esp32s3-camera-stream-smp").unwrap();
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
            assert!(output.contains(&"cargo:rustc-cfg=hal_provider=\"hardware\"".into()));
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
    fn native_camera_and_ble_bind_profile_resources() {
        for (name, capability, key, variable, default, alternate, invalid) in [
            (
                "esp32s3-camera",
                "camera",
                "device",
                "CAMERA_DEVICE",
                "/dev/video0",
                "/dev/video2",
                "/dev/../video0",
            ),
            (
                "esp32s3-ble",
                "ble",
                "scan-interface",
                "BLE_SCAN_INTERFACE",
                "bnep0",
                "bnep7",
                "bnep123456789012",
            ),
        ] {
            let mut profile = Profile::resolve(&root(), name).unwrap();
            assert_eq!(profile.data[capability][key], default);
            profile.data[capability][key] = json!(alternate);
            profile.validate_resources().unwrap();
            assert!(profile
                .cargo_output(&root(), capability, "nuttx")
                .unwrap()
                .contains(&format!("cargo:rustc-env={variable}={alternate}")));

            profile.data[capability][key] = json!(invalid);
            assert!(profile.validate_resources().is_err());
            profile.data[capability][key] = json!(alternate);
            profile.data["hal"][capability] = json!("mock");
            assert!(profile.validate_resources().is_err());

            profile.data["hal"][capability] = json!("nuttx");
            profile.data.as_object_mut().unwrap().remove(capability);
            let profile = Profile::from_data(profile.data, profile.sources);
            profile.validate_resources().unwrap();
            assert_eq!(profile.json().unwrap()[capability][key], default);
            assert!(profile
                .cargo_output(&root(), capability, "nuttx")
                .unwrap()
                .contains(&format!("cargo:rustc-env={variable}={default}")));
        }
    }
    #[test]
    fn every_tracked_profile_resolves() {
        let root = root();
        for dir in ["platform/profiles", "platform/nuttx/platforms"] {
            for file in fs::read_dir(root.join(dir)).unwrap() {
                let p = file.unwrap().path();
                if p.extension().is_some_and(|s| s == "toml") {
                    Profile::resolve(&root, p.file_stem().unwrap().to_str().unwrap()).unwrap();
                }
            }
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
        assert!(imu.contains(&"cargo:rustc-cfg=hal_provider=\"simulation\"".into()));
        assert!(!imu.contains(&"cargo:rustc-cfg=hal_enabled".into()));
        assert!(!imu.contains(&"cargo:rustc-cfg=hal_mock".into()));
        let gnss = profile.cargo_output(&root, "gnss", "linux").unwrap();
        assert!(gnss.contains(&"cargo:rustc-cfg=hal_provider=\"simulation\"".into()));
        let absent = profile.cargo_output(&root, "camera", "linux").unwrap();
        assert!(!absent.iter().any(|line| line.contains("rustc-cfg=hal_")));
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
}
