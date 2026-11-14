//! Native Kconfig inputs, mandatory constraints, and finalized build identity.
use crate::{
    boot,
    build::{kconfig_value, App, Options},
    hash, hash_bytes,
    profile::Profile,
    read, reuse, Result,
};
use serde_json::{json, Value};
use std::{collections::BTreeMap, fs, path::Path, process::Command};

fn array(value: &Value, key: &str) -> Result<Vec<String>> {
    match value.get(key) {
        None => Ok(vec![]),
        Some(v) => v
            .as_array()
            .ok_or_else(|| format!("{key} must be an array"))?
            .iter()
            .map(|v| {
                v.as_str()
                    .filter(|s| !s.is_empty())
                    .map(str::to_owned)
                    .ok_or_else(|| format!("{key} requires nonempty strings"))
            })
            .collect(),
    }
}
pub(crate) fn line(key: &str, value: &str) -> String {
    if value == "n" {
        format!("# CONFIG_{key} is not set")
    } else {
        format!("CONFIG_{key}={value}")
    }
}
fn preparation_owned(name: &str) -> bool {
    [
        "APPS_DIR",
        "BASE_DEFCONFIG",
        "CYW43439_FIRMWARE_BIN_PATH",
        "CYW43439_FIRMWARE_LEN",
        "CYW43439_BLUETOOTH_FIRMWARE_BIN_PATH",
    ]
    .contains(&name)
}

pub(crate) fn assignment(text: &str) -> Option<(&str, &str)> {
    if let Some(key) = text
        .strip_prefix("# CONFIG_")
        .and_then(|s| s.strip_suffix(" is not set"))
    {
        return Some((key, "n"));
    }
    text.strip_prefix("CONFIG_")?.split_once('=')
}

#[derive(Debug, Clone)]
struct Request {
    value: String,
    source: String,
    command_line: bool,
}

impl Request {
    fn json(&self) -> Value {
        json!({"value":self.value,"source":self.source,"command_line":self.command_line})
    }
}

fn explicit_requests(
    profile: &Profile,
    options: &Options,
) -> Result<BTreeMap<String, Vec<Request>>> {
    let mut history = BTreeMap::<String, Vec<Request>>::new();
    if let Some(fragment) = profile.native_config() {
        for (index, raw) in fragment.contents.lines().enumerate() {
            let raw = raw.trim();
            if raw.is_empty() || (raw.starts_with('#') && !raw.starts_with("# CONFIG_")) {
                continue;
            }
            if raw.starts_with("# CONFIG_")
                && !raw.ends_with(" is not set")
                && !raw.ends_with(" not set")
            {
                // Comments mentioning a symbol need not be assignments.
                continue;
            }
            let source = format!("{}:{}", fragment.path.display(), index + 1);
            let (key, value) = assignment(raw)
                .ok_or_else(|| format!("{source}: expected a native Kconfig assignment"))?;
            if key.is_empty()
                || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
                || !kconfig_value(value)
            {
                return Err(format!("{source}: malformed Kconfig assignment for {key}"));
            }
            let entries = history.entry(key.into()).or_default();
            if let Some(previous) = entries.last() {
                if previous.value != value {
                    return Err(format!(
                        "{source}: contradictory CONFIG_{key} request from {}",
                        previous.source
                    ));
                }
            }
            entries.push(Request {
                value: value.into(),
                source,
                command_line: false,
            });
        }
    }
    for (key, value) in options.kconfig()? {
        history.entry(key).or_default().push(Request {
            value,
            source: "command line".into(),
            command_line: true,
        });
    }
    Ok(history)
}

pub fn configure(path: &Path, values: &BTreeMap<String, String>) -> Result<()> {
    configure_in_order(path, values, &BTreeMap::new())
}

fn configure_in_order(
    path: &Path,
    values: &BTreeMap<String, String>,
    requests: &BTreeMap<String, Vec<Request>>,
) -> Result<()> {
    for (key, value) in values {
        if !key
            .bytes()
            .all(|b| b.is_ascii_alphabetic() || b.is_ascii_digit() || b == b'_')
            || key.is_empty()
            || value.contains(['\n', '\r', '\0'])
        {
            return Err(format!("invalid Kconfig assignment for {key}"));
        }
    }
    let text = read(path)?;
    let mut result = String::new();
    for current in text.lines() {
        let key = assignment(current).map(|(key, _)| key);
        if !key.is_some_and(|k| values.contains_key(k)) {
            result.push_str(current);
            result.push('\n');
        }
    }
    // Kconfiglib picks the last enabled member of a choice. Write ordinary
    // requests after defaults so the result does not depend on symbol names.
    for priority in 0..=2 {
        for (key, value) in values.iter().filter(|(key, _)| {
            requests
                .get(*key)
                .and_then(|entries| entries.last())
                .map_or(0, |request| 1 + u8::from(request.command_line))
                == priority
        }) {
            result.push_str(&line(key, value));
            result.push('\n');
        }
    }
    fs::write(path, result).map_err(|e| e.to_string())
}
pub fn required(config: &str, requirements: &[String], forbid: &str) -> Result<()> {
    for value in requirements {
        if !config.lines().any(|line| line == value) {
            return Err(format!("unresolved NuttX requirement: {value}"));
        }
    }
    if !forbid.is_empty() {
        let regex = regex::RegexBuilder::new(forbid)
            .multi_line(true)
            .build()
            .map_err(|e| e.to_string())?;
        if regex.is_match(config) {
            return Err("forbidden NuttX option enabled".into());
        }
    }
    Ok(())
}
pub(crate) const ESP32S3_KERNEL_OFFSET: u64 = 0x10000;
pub(crate) const ESP32S3_KERNEL_SIZE: u64 = 0x80000;
// Match the pinned ESP32-S3 PREBUILD formula before compiling Rust helpers.
pub(crate) const ESP32S3_USERSPACE: u64 = 0x3c000030 + ESP32S3_KERNEL_SIZE;

fn standard(profile: &Profile, protected: bool) -> BTreeMap<String, String> {
    let mut v = BTreeMap::new();
    for key in [
        "EXAMPLES_NXRS_STD_APP",
        "FS_LARGEFILE",
        "DEV_URANDOM",
        "SCHED_WAITPID",
        "SCHED_HAVE_PARENT",
        "SCHED_CHILD_STATUS",
    ] {
        v.insert(key.into(), "y".into());
    }
    if !protected {
        v.insert("TLS_GLOBAL_KEYS".into(), "y".into());
    }
    if profile.data.get("init").is_none() {
        for key in ["NSH_DISABLEBG", "NSH_ARGCAT"] {
            v.insert(key.into(), "y".into());
        }
    }
    for (key, value) in [
        ("TLS_NELEM", "16"),
        ("TLS_DTOR_ITERATIONS", "4"),
        ("TLS_NCLEANUP", "16"),
        ("RR_INTERVAL", "10"),
    ] {
        v.insert(key.into(), value.into());
    }
    v
}

fn protected_config() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("BUILD_PROTECTED".into(), "y".into()),
        ("ESP32S3_WCL".into(), "y".into()),
        ("ESP32S3_APP_FORMAT_LEGACY".into(), "y".into()),
        ("RAW_BINARY".into(), "y".into()),
        (
            "ESP32S3_KERNEL_OFFSET".into(),
            format!("{ESP32S3_KERNEL_OFFSET:#x}"),
        ),
        (
            "ESP32S3_KERNEL_IMAGE_SIZE".into(),
            format!("{ESP32S3_KERNEL_SIZE:#x}"),
        ),
        ("NUTTX_USERSPACE".into(), format!("{ESP32S3_USERSPACE:#x}")),
        (
            "PASS1_BUILDIR".into(),
            "\"boards/xtensa/esp32s3/common/kernel\"".into(),
        ),
    ])
}

/// Reject qualification requests that contradict native wiring or required build settings.
/// This is also used by dry runs, before preparing an output directory or an SDK.
pub fn validate_overrides(profile: &Profile, app: &App, options: &Options) -> Result<()> {
    check_ownership(profile, app, &explicit_requests(profile, options)?)
}

fn check_ownership(
    profile: &Profile,
    app: &App,
    requested: &BTreeMap<String, Vec<Request>>,
) -> Result<()> {
    if requested.is_empty() {
        return Ok(());
    }
    let check = |key: &str, expected: &str, owner: &str| -> Result<()> {
        if let Some(request) = requested
            .get(key)
            .into_iter()
            .flatten()
            .find(|request| request.value != expected)
        {
            return Err(format!(
                "Kconfig override for {key} conflicts with {owner} requirement {}",
                format_args!("{} ({})", line(key, expected), request.source)
            ));
        }
        Ok(())
    };
    let protected = profile.data.pointer("/build/protected-runtime") == Some(&json!(true));
    for (key, value) in standard(profile, protected) {
        check(&key, &value, "builder")?;
    }
    check("SIG_DEFAULT", "n", "builder")?;
    if let Some((key, entries)) = requested.iter().find(|(key, _)| preparation_owned(key)) {
        return Err(format!(
            "{}: CONFIG_{key} is owned by NuttX board preparation",
            entries[0].source
        ));
    }
    if protected {
        for (key, value) in protected_config() {
            check(&key, &value, "protected build")?;
        }
        for key in ["BUILD_2PASS", "ARCH_USE_MPU"] {
            check(key, "y", "protected build")?;
        }
        check("TLS_GLOBAL_KEYS", "n", "protected build")?;
    } else {
        check("BUILD_FLAT", "y", "builder")?;
    }
    for (key, value) in
        boot::init_config(profile, &app.command, app.priority.into(), app.stack.into())?
    {
        check(&key, &value, "application init")?;
    }
    if profile.data.get("mcuboot").is_some() {
        check("ESP32S3_APP_FORMAT_MCUBOOT", "y", "signed build")?;
        check("ESP32S3_FLASH_16M", "y", "signed build")?;
        for (key, value) in boot::application_config(profile)? {
            check(&key, &value, "signed build")?;
        }
    }
    if let Some(settings) = profile
        .data
        .pointer("/resolved-nuttx-resources/kconfig")
        .and_then(Value::as_object)
    {
        for (key, value) in settings {
            check(
                key,
                value.as_str().ok_or("invalid BSP setting")?,
                "BSP resource",
            )?;
        }
    }
    let config = profile.data.get("kconfig").unwrap_or(&Value::Null);
    for requirement in array(config, "require")? {
        let (key, value) = if let Some(key) = requirement
            .strip_prefix("# CONFIG_")
            .and_then(|s| s.strip_suffix(" is not set"))
        {
            (key, "n")
        } else {
            requirement
                .strip_prefix("CONFIG_")
                .and_then(|s| s.split_once('='))
                .ok_or("invalid profile Kconfig requirement")?
        };
        check(key, value, "profile")?;
    }
    if let Some(forbid) = config.get("forbid-regex") {
        let forbid = forbid.as_str().ok_or("forbid-regex must be a string")?;
        if !forbid.is_empty() {
            let regex = regex::RegexBuilder::new(forbid)
                .multi_line(true)
                .build()
                .map_err(|e| e.to_string())?;
            for (key, request) in requested
                .iter()
                .flat_map(|(key, entries)| entries.iter().map(move |request| (key, request)))
            {
                if regex.is_match(&line(key, &request.value)) {
                    return Err(format!(
                        "Kconfig override for {key} is forbidden by the profile ({})",
                        request.source
                    ));
                }
            }
        }
    }
    Ok(())
}

pub(crate) struct Configuration {
    pub values: BTreeMap<String, String>,
    requests: BTreeMap<String, Vec<Request>>,
    requirements: Vec<String>,
    forbid: String,
    protected: bool,
}

impl Configuration {
    pub fn assemble(p: &Profile, app: &App, protected: bool, options: &Options) -> Result<Self> {
        let requests = explicit_requests(p, options)?;
        check_ownership(p, app, &requests)?;
        let signed = p.data.get("mcuboot").is_some();
        let mut values = BTreeMap::new();
        for key in [
            "EXAMPLES_HELLO",
            "TESTING_OSTEST",
            "TESTING_GETPRIME",
            "ARCH_FPU",
            "SIG_DEFAULT",
        ] {
            values.insert(key.into(), "n".into());
        }
        let k = p
            .data
            .get("kconfig")
            .filter(|k| k.is_object())
            .ok_or("missing kconfig table")?;
        for (key, value) in [("enable", "y"), ("disable", "n")] {
            for symbol in array(k, key)? {
                values.insert(symbol, value.into());
            }
        }
        for item in array(k, "set")? {
            let (key, value) = item
                .split_once('=')
                .ok_or("invalid Kconfig set assignment")?;
            values.insert(key.into(), value.into());
        }
        let mut requirements = array(k, "require")?;
        if signed {
            values.insert("ESP32S3_APP_FORMAT_MCUBOOT".into(), "y".into());
            requirements.extend([
                "CONFIG_ESP32S3_APP_FORMAT_MCUBOOT=y".into(),
                "CONFIG_ESP32S3_FLASH_16M=y".into(),
            ]);
            let v = boot::application_config(p)?;
            requirements.extend(v.iter().map(|(k, v)| line(k, v)));
            values.extend(v);
        }
        let init = boot::init_config(p, &app.command, app.priority.into(), app.stack.into())?;
        requirements.extend(init.iter().map(|(k, v)| line(k, v)));
        values.extend(init);
        for (key, entries) in &requests {
            values.insert(key.clone(), entries.last().unwrap().value.clone());
        }
        let standard = standard(p, protected);
        requirements.extend(standard.iter().map(|(k, v)| line(k, v)));
        if protected {
            let mode = protected_config();
            requirements.extend(mode.iter().map(|(k, v)| line(k, v)));
            requirements.extend([
                "CONFIG_BUILD_2PASS=y".into(),
                "CONFIG_ARCH_USE_MPU=y".into(),
            ]);
            values.extend(mode);
        } else {
            requirements.push("CONFIG_BUILD_FLAT=y".into());
        }
        values.extend(standard);
        let forbid = match k.get("forbid-regex") {
            None => String::new(),
            Some(v) => v.as_str().ok_or("forbid-regex must be a string")?.into(),
        };
        if !forbid.is_empty() {
            regex::Regex::new(&forbid).map_err(|e| e.to_string())?;
        }
        Ok(Self {
            values,
            requests,
            requirements,
            forbid,
            protected,
        })
    }

    pub fn add_provenance(&self, profile: &Profile, report: &mut Value) -> Result<()> {
        let symbols = report["symbols"]
            .as_object_mut()
            .ok_or("missing Kconfig catalog")?;
        let bsp = profile.data.pointer("/resolved-nuttx-resources/kconfig");
        let selections = profile.data.pointer("/resolved-nuttx-resources/selections");
        for (name, symbol) in symbols {
            let mut provenance = Vec::new();
            let exists = |field: &str| profile.data["kconfig"][field].as_array();
            let bsp_owned = bsp.is_some_and(|settings| settings.get(name).is_some());
            for (field, value) in [("enable", "y"), ("disable", "n")] {
                if !bsp_owned
                    && exists(field)
                        .is_some_and(|items| items.iter().any(|item| item == name.as_str()))
                {
                    provenance.push(json!({"kind":"profile default", "source":profile.source_of("kconfig"), "value":value}));
                }
            }
            for item in exists("set").into_iter().flatten() {
                if let Some((key, value)) = item.as_str().and_then(|s| s.split_once('=')) {
                    if !bsp_owned && key == name {
                        provenance.push(json!({"kind":"profile default", "source":profile.source_of("kconfig"), "value":value}));
                    }
                }
            }
            for request in self.requests.get(name).into_iter().flatten() {
                provenance.push(json!({"kind":"explicit request","source":request.source,"value":request.value}));
            }
            let requirement = self
                .requirements
                .iter()
                .filter_map(|line| assignment(line))
                .find(|(key, _)| key == name)
                .map(|(_, value)| value);
            if preparation_owned(name) {
                symbol["ownership"] = json!({"owner":"NuttX board preparation"});
            } else if bsp_owned {
                let resource = selections
                    .and_then(Value::as_object)
                    .and_then(|selections| {
                        selections
                            .iter()
                            .find(|(_, selected)| selected["kconfig"].get(name).is_some())
                    });
                let (owner, source) = resource.map_or_else(
                    || ("BSP resource".to_owned(), None),
                    |(id, _)| {
                        (
                            format!("resources.{id}.kconfig.{name}"),
                            profile.source_of(&format!("resources.{id}.kconfig.{name}")),
                        )
                    },
                );
                symbol["ownership"] = json!({"owner":owner,"source":source});
            } else if requirement.is_some()
                || name == "SIG_DEFAULT"
                || (self.protected && name == "TLS_GLOBAL_KEYS")
            {
                let source = exists("require")
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .filter_map(assignment)
                    .any(|(key, _)| key == name)
                    .then(|| profile.source_of("kconfig"))
                    .flatten();
                symbol["ownership"] = json!({
                    "owner":if source.is_some() {"profile requirement"} else {"build contract"},
                    "source":source, "value":requirement
                });
            }
            if !provenance.is_empty() {
                symbol["provenance"] = json!(provenance);
            }
        }
        Ok(())
    }

    pub fn validate(&self, profile: &Profile, config: &str) -> Result<()> {
        required(config, &self.requirements, &self.forbid)?;
        nxrs_build_support::nuttx_resources::validate_config(&profile.data, config)?;
        if self.protected
            && config
                .lines()
                .any(|line| line == "CONFIG_TLS_GLOBAL_KEYS=y")
        {
            return Err(
                "protected direct-init qualification requires group-local pthread keys".into(),
            );
        }
        if config.lines().any(|s| s == "CONFIG_SIG_DEFAULT=y") {
            return Err("unqualified SIG_DEFAULT configuration".into());
        }
        Ok(())
    }

    pub fn evidence(&self, config: &Finalized) -> Value {
        let effective: BTreeMap<_, _> = config.config.lines().filter_map(assignment).collect();
        let requests: serde_json::Map<_, _> = self.requests.iter().map(|(name, entries)| {
            let history = entries.iter().map(Request::json).collect::<Vec<_>>();
            (name.clone(), json!({"history":history,"effective":effective.get(name.as_str()).copied().unwrap_or("n")}))
        }).collect();
        json!({"effective_config_sha256":config.effective_config_sha256,"requests":requests})
    }

    pub fn validate_context(&self, initial: &str, prepared: &str) -> Result<()> {
        let initial: BTreeMap<_, _> = initial.lines().filter_map(assignment).collect();
        let prepared: BTreeMap<_, _> = prepared.lines().filter_map(assignment).collect();
        for (name, entries) in &self.requests {
            let previous = initial.get(name.as_str()).copied().unwrap_or("n");
            let current = prepared.get(name.as_str()).copied().unwrap_or("n");
            if previous != current {
                let requested = entries.last().ok_or("missing explicit request")?;
                return Err(format!(
                    "{}: CONFIG_{name} requested {:?}; Make context changed the effective value from {previous:?} to {current:?}",
                    requested.source, requested.value,
                ));
            }
        }
        Ok(())
    }
}

pub(crate) fn seed(nuttx: &Path, board: &str, command: impl Fn(&str) -> Command) -> Result<String> {
    crate::output(
        command("bash")
            .current_dir(nuttx)
            .arg("tools/configure.sh")
            .args(["-l", board, "KCONFIG_OLDDEFCONFIG=olddefconfig"])
            .env_remove("KCONFIG_CONFIG"),
    )?;
    read(&nuttx.join(".config"))
}

/// Configure only the native inputs. Make supplies the exact Kconfig environment
/// and generates the Apps and board Kconfig entries before starting the adapter.
pub(crate) fn resolve(
    root: &Path,
    nuttx: &Path,
    out: &Path,
    seed: &str,
    configuration: &Configuration,
    catalog: bool,
    command: impl Fn(&str) -> Command,
) -> Result<Value> {
    let temporary = crate::TempDir::new(out)?;
    let query = temporary.0.join("requests.json");
    let report = temporary.0.join("report.json");
    let requests = configuration
        .requests
        .iter()
        .map(|(name, history)| {
            let history = history.iter().map(Request::json).collect::<Vec<_>>();
            (name.clone(), json!(history))
        })
        .collect::<serde_json::Map<_, _>>();
    crate::write_json(&query, &json!({"requests": requests, "catalog": catalog}))?;
    let adapter = root.join("tools/build/nuttx/kconfig.py");
    let adapter = adapter
        .to_str()
        .ok_or("Kconfig adapter path must be UTF-8")?;
    fs::write(nuttx.join(".config"), seed).map_err(|e| e.to_string())?;
    configure_in_order(
        &nuttx.join(".config"),
        &configuration.values,
        &configuration.requests,
    )?;
    // Set the recipe operation only. Keep NuttX's KCONFIG_ENV and
    // apps_preconfig so the adapter sees the same tree as olddefconfig.
    let output = command("make")
        .current_dir(nuttx)
        .arg("olddefconfig")
        .arg(format!(
            "KCONFIG_OLDDEFCONFIG=python3 {}",
            crate::shell_quote(adapter)
        ))
        .env_remove("KCONFIG_CONFIG")
        .env("NXRS_KCONFIG_REQUESTS", &query)
        .env("NXRS_KCONFIG_REPORT", &report)
        .output()
        .map_err(|error| format!("could not start NuttX olddefconfig: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "NuttX olddefconfig failed:\n{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    crate::read_json(&report)
}

pub fn check_report(report: &Value) -> Result<()> {
    let problems = report["problems"]
        .as_array()
        .ok_or("missing native configuration diagnostics")?;
    if problems.is_empty() {
        return Ok(());
    }
    Err(problems
        .iter()
        .map(|message| message.as_str().unwrap_or("invalid Kconfig diagnostic"))
        .collect::<Vec<_>>()
        .join("\n"))
}

/// Make relabels the defconfig as dirty when generating config.h.
pub(crate) fn verify_preview(preview: &str, prepared: &str) -> Result<()> {
    fn values(config: &str) -> BTreeMap<&str, &str> {
        config
            .lines()
            .filter_map(assignment)
            .filter(|(key, _)| *key != "BASE_DEFCONFIG")
            .collect()
    }
    let preview = values(preview);
    let prepared = values(prepared);
    if let Some(key) = preview
        .keys()
        .chain(prepared.keys())
        .find(|key| preview.get(**key) != prepared.get(**key))
    {
        return Err(format!(
            "saved configuration differs from preview at CONFIG_{key}"
        ));
    }
    Ok(())
}

pub(crate) struct Finalized {
    pub config: String,
    pub effective_config_sha256: String,
    pub config_header_sha256: String,
    pub headers_sha256: String,
}

impl Finalized {
    pub fn capture(nuttx: &Path) -> Result<Self> {
        let config = read(&nuttx.join(".config"))?;
        let config_header_sha256 = hash(&nuttx.join("include/nuttx/config.h"))?;
        let headers = reuse::snapshot(nuttx, &["include"])?;
        let headers_sha256 = hash_bytes(&serde_json::to_vec(&headers).map_err(|e| e.to_string())?);
        Ok(Self {
            effective_config_sha256: hash_bytes(config.as_bytes()),
            config_header_sha256,
            headers_sha256,
            config,
        })
    }

    pub fn verify(&self, nuttx: &Path) -> Result<Self> {
        let current = Self::capture(nuttx)?;
        for (name, prepared, linked) in [
            (
                ".config",
                &self.effective_config_sha256,
                &current.effective_config_sha256,
            ),
            (
                "include/nuttx/config.h",
                &self.config_header_sha256,
                &current.config_header_sha256,
            ),
            (
                "configured NuttX headers",
                &self.headers_sha256,
                &current.headers_sha256,
            ),
        ] {
            if prepared != linked {
                return Err(format!(
                    "NuttX {name} changed after preparation ({prepared} -> {linked})"
                ));
            }
        }
        Ok(current)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn direct_application_init_does_not_require_a_shell() {
        let mut profile = Profile::from_data(json!({}), vec![]);
        assert_eq!(standard(&profile, false)["NSH_ARGCAT"], "y");
        profile.data["init"] = json!({});
        let settings = standard(&profile, false);
        assert!(!settings.contains_key("NSH_ARGCAT"));
        assert!(!settings.contains_key("NSH_DISABLEBG"));
        assert_eq!(settings["TLS_GLOBAL_KEYS"], "y");
        assert!(!standard(&profile, true).contains_key("TLS_GLOBAL_KEYS"));
    }

    #[test]
    fn configuration_and_header_drift_cannot_replace_the_prepared_snapshot() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let nuttx = temp.0.join("nuttx");
        let config = nuttx.join(".config");
        let header = nuttx.join("include/nuttx/config.h");
        fs::create_dir_all(header.parent().unwrap()).unwrap();
        fs::write(&config, "CONFIG_MODE=1\n").unwrap();
        fs::write(&header, "#define CONFIG_MODE 1\n").unwrap();
        let authored = Finalized::capture(&nuttx).unwrap();

        // Context may change the input before compilation begins.
        fs::write(&config, "CONFIG_MODE=2\n").unwrap();
        fs::write(&header, "#define CONFIG_MODE 2\n").unwrap();
        let prepared = Finalized::capture(&nuttx).unwrap();
        assert_ne!(
            prepared.effective_config_sha256,
            authored.effective_config_sha256
        );
        let receipt = temp.0.join("resolved.config");
        fs::write(&receipt, &prepared.config).unwrap();
        assert!(prepared.verify(&nuttx).is_ok());

        for (path, changed, expected) in [
            (&config, "CONFIG_MODE=3\n", ".config"),
            (&header, "#define CONFIG_MODE 3\n", "include/nuttx/config.h"),
        ] {
            let original = fs::read(path).unwrap();
            fs::write(path, changed).unwrap();
            assert!(prepared.verify(&nuttx).err().unwrap().contains(expected));
            assert_eq!(read(&receipt).unwrap(), "CONFIG_MODE=2\n");
            fs::write(path, original).unwrap();
        }
        fs::write(nuttx.join("include/generated.h"), "new native input").unwrap();
        assert!(prepared
            .verify(&nuttx)
            .err()
            .unwrap()
            .contains("configured NuttX headers"));
    }
}
