//! One resolver for CLI builds and HAL build scripts.
use crate::{read, Result};
use regex::Regex;
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs,
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
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Detector {
    pub model: String,
    pub stack_bytes: usize,
    pub sample_ms: u64,
    pub compute_core: Option<u64>,
}

pub fn manifest(path: &Path) -> Result<Value> {
    let value: toml::Value =
        toml::from_str(&read(path)?).map_err(|e| format!("{}: {e}", path.display()))?;
    serde_json::to_value(value).map_err(|e| e.to_string())
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
fn sensor_stack(v: &Value) -> Result<u64> {
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
    pub fn resolve(root: &Path, name: &str) -> Result<Self> {
        Self::inherit(root, name, &[])
    }
    fn inherit(root: &Path, name: &str, chain: &[String]) -> Result<Self> {
        if chain.len() >= 16 || chain.iter().any(|s| s == name) {
            return Err(format!("platform inheritance cycle/depth: {name}"));
        }
        let path = profile_path(root, name)?;
        let mut child = manifest(&path)?;
        if child.get("hal-features").is_some() {
            return Err("retired hal-features; platform must own [hal] bindings".into());
        }
        let parent = child.as_object_mut().unwrap().remove("extends");
        let mut resolved = if let Some(parent) = parent {
            let parent = parent.as_str().ok_or("extends must name one platform")?;
            let mut chain = chain.to_vec();
            chain.push(name.into());
            Self::inherit(root, parent, &chain)?
        } else {
            Self {
                data: json!({"hal":{}}),
                sources: vec![],
            }
        };
        let bindings = child
            .as_object_mut()
            .unwrap()
            .remove("hal")
            .unwrap_or(json!({}));
        let bindings = bindings.as_object().ok_or("[hal] must be a table")?;
        for (k, v) in bindings {
            resolved.data["hal"][k] = v.clone();
        }
        for (k, v) in child.as_object().unwrap() {
            resolved.data[k] = v.clone();
        }
        resolved.data["name"] = json!(name);
        resolved.sources.push(path);
        for (capability, provider) in resolved.data["hal"].as_object().unwrap() {
            if !CAPABILITIES.contains(&capability.as_str()) {
                return Err(format!("unknown capability: {capability}"));
            }
            let provider = provider.as_str().ok_or("invalid HAL binding")?;
            let m = manifest(&root.join("hal").join(capability).join("Cargo.toml"))?;
            let declared = m.pointer("/package/metadata/nxrs/platforms");
            if matches!(provider, "std" | "native")
                || (provider != "none"
                    && !declared
                        .and_then(Value::as_array)
                        .is_some_and(|p| p.iter().any(|x| x == provider)))
            {
                return Err(format!(
                    "{name}: unsupported {capability} implementation: {provider}"
                ));
            }
        }
        resolved.validate_resources()?;
        resolved.configure_cpus()?;
        Ok(resolved)
    }
    pub fn name(&self) -> &str {
        self.data["name"].as_str().unwrap()
    }
    pub fn binding(&self, capability: &str) -> &str {
        self.data["hal"][capability].as_str().unwrap_or("none")
    }
    pub fn json(&self) -> Value {
        let mut value = self.data.clone();
        value["sources"] = json!(self.sources);
        value
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
        for (name, provider) in self.data["hal"].as_object().unwrap() {
            let p = provider.as_str().unwrap();
            if !["none", "mock"].contains(&p)
                && !(p == os && (HOST_OS.contains(&p) || p == "nuttx"))
            {
                return Err(format!("{name}/{p} does not support target OS {os}"));
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
        self.network_initialization()?;
        for capability in ["gnss", "imu"] {
            let Some(v) = self.data.get(capability) else {
                if self.binding(capability) == "nuttx" {
                    return Err(format!("{capability} requires device configuration"));
                } else {
                    continue;
                }
            };
            if self.binding(capability) != "nuttx" {
                return Err(format!(
                    "{capability} device configuration requires its NuttX binding"
                ));
            }
            sensor_stack(v)?;
            if capability == "gnss" {
                if !keys(v, &["device", "baud"], &["stack-bytes"])
                    || !device(&v["device"])
                    || !v["baud"].as_u64().is_some_and(|n| {
                        [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600].contains(&n)
                    })
                {
                    return Err("invalid GNSS UART binding".into());
                }
            } else if !keys(
                v,
                &["driver", "device", "address", "frequency"],
                &["stack-bytes", "irq-device"],
            ) || v["driver"] != "bmi270"
                || !device(&v["device"])
                || !v["address"]
                    .as_u64()
                    .is_some_and(|n| [0x68, 0x69].contains(&n))
                || !v["frequency"]
                    .as_u64()
                    .is_some_and(|n| [100000, 400000].contains(&n))
                || v.get("irq-device").is_some_and(|v| v != "" && !device(v))
            {
                return Err("invalid BMI270 I2C binding".into());
            }
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
                config["forbid-regex"]=json!(format!("(?:{previous})|^CONFIG_(ARCH_PERF_EVENTS|PERF_OVERFLOW_CORRECTION|TIMER_ARCH|ALARM_ARCH)=y$"));
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
            "gnss" | "imu" if self.binding(capability) == "nuttx" => {
                let c = &self.data[capability];
                let mut c = c.clone();
                c["stack-bytes"] = json!(sensor_stack(&c)?);
                if capability == "imu" && c.get("irq-device").is_none() {
                    c["irq-device"] = json!("")
                }
                let keys = if capability == "gnss" {
                    vec!["device", "baud", "stack-bytes"]
                } else {
                    vec![
                        "device",
                        "address",
                        "frequency",
                        "stack-bytes",
                        "irq-device",
                    ]
                };
                for key in keys {
                    let value = c[key]
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| c[key].to_string());
                    env(
                        &format!(
                            "{}_{}",
                            capability.to_uppercase(),
                            key.replace('-', "_").to_uppercase()
                        ),
                        value,
                    )
                }
            }
            "wifi" if self.binding(capability) == "nuttx" => {
                env("WIFI_INITIALIZATION", self.network_initialization()?.into())
            }
            "console" if self.binding(capability) != "none" => env(
                "CONSOLE_DEVICE",
                self.data["console"]["device"].as_str().unwrap().into(),
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
            self.sources
                .iter()
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
        match self.binding(capability) {
            "none" => {}
            "mock" => output.push("cargo:rustc-cfg=hal_mock".into()),
            _ => output.push("cargo:rustc-cfg=hal_enabled".into()),
        }
        Ok(output)
    }
    /// Inventory of all profile-bound NuttX shims, for diagnostics only.
    pub fn native_helpers(&self, root: &Path) -> Result<Vec<PathBuf>> {
        self.native_helpers_selected(root, None)
    }
    /// Build only private helpers for HAL packages consumed by this application.
    pub fn native_helpers_for(
        &self, root: &Path, selected: &crate::selection::CargoSelection
    ) -> Result<Vec<PathBuf>> {
        self.native_helpers_selected(root, Some(&selected.packages))
    }
    fn native_helpers_selected(
        &self, root: &Path, selected: Option<&BTreeSet<String>>
    ) -> Result<Vec<PathBuf>> {
        let mut sources = vec![];
        for (capability, provider) in self.data["hal"].as_object().unwrap() {
            if provider == "nuttx" && selected.is_none_or(|s| s.contains(&format!("nxrs-{capability}"))) {
                let dir = root.join("hal").join(capability).join("src/platform/nuttx/ffi");
                if dir.is_dir() {
                    for e in fs::read_dir(&dir).map_err(|e| e.to_string())? {
                        let p = e.map_err(|e| e.to_string())?.path();
                        if p.extension().is_some_and(|x| x == "c") { sources.push(p) }
                    }
                }
            }
        }
        sources.sort();
        Ok(sources)
    }
    /// Model identity is profile-owned; activation follows Cargo's selected graph.
    pub fn model_features(&self, selected: &crate::selection::CargoSelection) -> Result<Vec<String>> {
        if let Some(detector) = self.detector()? {
            if selected.contains("nxrs-detector") {
                return Ok(vec![format!("nxrs-detector/{}", if detector.model == "pico" {
                    "pico-model"
                } else {
                    "person-model"
                })]);
            }
        }
        Ok(vec![])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
    fn only_selected_hal_packages_contribute_private_native_helpers() {
        let root = root();
        let profile = Profile::resolve(&root, "esp32s3-camera-stream").unwrap();
        let camera = crate::selection::CargoSelection {
            packages: BTreeSet::from(["nxrs-camera".into()])
        };
        let helpers = profile.native_helpers_for(&root, &camera).unwrap();
        assert!(!helpers.is_empty());
        assert!(helpers.iter().all(|p| p.starts_with(root.join("hal").join("camera"))));
        let unused = crate::selection::CargoSelection { packages: BTreeSet::new() };
        assert!(profile.native_helpers_for(&root, &unused).unwrap().is_empty());
    }

    #[test]
    fn detector_models_are_profile_owned_but_cargo_consumption_gated() {
        let root = root();
        let profile = Profile::resolve(&root, "esp32s3-camera-stream-vww").unwrap();
        let unused = crate::selection::CargoSelection { packages: BTreeSet::new() };
        assert!(profile.model_features(&unused).unwrap().is_empty());
        let selected = crate::selection::CargoSelection {
            packages: BTreeSet::from(["nxrs-detector".into()])
        };
        assert_eq!(profile.model_features(&selected).unwrap(), vec!["nxrs-detector/person-model"]);
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
}
