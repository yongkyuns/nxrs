//! Resource-mutex configuration policy for ordinary NuttX builds.
//!
//! This owns native configuration, not application scheduling or a mutex runtime.
use crate::{
    build::{App, Options},
    deployment,
    nuttx_config::{assignment, line},
    profile::{manifest, Profile},
    Result,
};
use serde_json::{json, Value};
use std::{collections::BTreeMap, str::FromStr};

/// Deliberately different experimental controls; neither is a product default.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QualificationMutexPolicy {
    NoInherit,
    KernelPiDisabled,
}

impl QualificationMutexPolicy {
    pub fn name(self) -> &'static str {
        match self {
            Self::NoInherit => "no-inherit",
            Self::KernelPiDisabled => "kernel-pi-disabled",
        }
    }
}

impl FromStr for QualificationMutexPolicy {
    type Err = String;

    fn from_str(value: &str) -> Result<Self> {
        match value {
            "no-inherit" => Ok(Self::NoInherit),
            "kernel-pi-disabled" => Ok(Self::KernelPiDisabled),
            _ => Err("--qualification-mutex-policy must be no-inherit or kernel-pi-disabled; ordinary builds use inherit".into()),
        }
    }
}

pub(crate) fn validate_control(options: &Options) -> Result<()> {
    match (
        options.qualification_mutex_policy,
        options.qualification_mutex_reason.as_deref(),
    ) {
        (None, None) => Ok(()),
        (Some(_), Some(reason))
            if !reason.trim().is_empty()
                && reason.len() <= 512
                && !reason.chars().any(char::is_control) => Ok(()),
        _ => Err("a mutex qualification control requires both --qualification-mutex-policy and a nonempty, single-line --qualification-mutex-reason (at most 512 bytes)".into()),
    }
}

#[derive(Debug, Clone)]
pub(crate) struct MutexPolicy {
    control: Option<QualificationMutexPolicy>,
    reason: Option<String>,
}

impl MutexPolicy {
    pub fn resolve(app: &App, options: &Options) -> Result<Self> {
        validate_control(options)?;
        if options.qualification_mutex_policy.is_some() {
            let metadata = manifest(&app.manifest)?;
            deployment::validate_metadata(&metadata)?;
            if metadata.pointer("/package/metadata/nxrs/qualification-only") != Some(&json!(true)) {
                return Err("mutex qualification controls require package.metadata.nxrs.qualification-only = true; product builds require priority inheritance".into());
            }
        }
        Ok(Self {
            control: options.qualification_mutex_policy,
            reason: options.qualification_mutex_reason.clone(),
        })
    }

    pub fn name(&self) -> &'static str {
        self.control
            .map_or("inherit", QualificationMutexPolicy::name)
    }

    fn inheritance(&self) -> bool {
        self.control != Some(QualificationMutexPolicy::KernelPiDisabled)
    }

    pub fn values(&self) -> BTreeMap<String, String> {
        let inherited_mutex = self.control.is_none();
        let mut values = BTreeMap::from([
            (
                "PRIORITY_INHERITANCE".into(),
                if self.inheritance() { "y" } else { "n" }.into(),
            ),
            (
                "PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT".into(),
                if inherited_mutex { "y" } else { "n" }.into(),
            ),
            (
                "PTHREAD_MUTEX_DEFAULT_PRIO_NONE".into(),
                if inherited_mutex { "n" } else { "y" }.into(),
            ),
        ]);
        if self.inheritance() {
            // True resource mutexes have one owner. Embedded bookkeeping avoids
            // a shared holder pool whose exhaustion panics inside the kernel.
            values.insert("SEM_PREALLOCHOLDERS".into(), "0".into());
        }
        values
    }

    pub fn check_request(&self, key: &str, value: &str, source: &str) -> Result<()> {
        if let Some(expected) = self.values().get(key).filter(|expected| *expected != value) {
            return Err(format!(
                "{source}: CONFIG_{key}={value} conflicts with the NXRS resource-mutex policy {} (requires {}); use an explicit qualification control only for a qualification-only entry",
                self.name(), line(key, expected),
            ));
        }
        Ok(())
    }

    /// Legacy profile defaults must not silently hide a contradictory policy.
    /// Fragments and CLI history are checked separately by Configuration.
    pub fn check_profile(&self, profile: &Profile) -> Result<()> {
        let config = &profile.data["kconfig"];
        let items = |field: &str| -> Result<Vec<&str>> {
            match config.get(field) {
                None => Ok(Vec::new()),
                Some(value) => value
                    .as_array()
                    .ok_or_else(|| format!("kconfig.{field} must be an array"))?
                    .iter()
                    .map(|value| {
                        value
                            .as_str()
                            .ok_or_else(|| format!("kconfig.{field} requires strings"))
                    })
                    .collect(),
            }
        };
        for (field, value) in [("enable", "y"), ("disable", "n")] {
            for key in items(field)? {
                self.check_request(key, value, &format!("profile kconfig.{field}"))?;
            }
        }
        for item in items("set")? {
            let (key, value) = item
                .split_once('=')
                .ok_or("invalid Kconfig set assignment")?;
            self.check_request(key, value, "profile kconfig.set")?;
        }
        for item in items("require")? {
            let (key, value) = assignment(item).ok_or("invalid profile Kconfig requirement")?;
            self.check_request(key, value, "profile kconfig.require")?;
        }
        if let Some(value) = config.get("forbid-regex") {
            let pattern = value.as_str().ok_or("forbid-regex must be a string")?;
            if !pattern.is_empty() {
                let regex = regex::RegexBuilder::new(pattern)
                    .multi_line(true)
                    .build()
                    .map_err(|e| e.to_string())?;
                let expected = self
                    .values()
                    .iter()
                    .map(|(key, value)| line(key, value) + "\n")
                    .collect::<String>();
                if regex.is_match(&expected) {
                    return Err(format!("profile kconfig.forbid-regex conflicts with the NXRS resource-mutex policy {}", self.name()));
                }
            }
        }
        Ok(())
    }

    pub fn validate_config(&self, config: &str) -> Result<()> {
        let effective: BTreeMap<_, _> = config.lines().filter_map(assignment).collect();
        for (key, expected) in self.values() {
            // Kconfig omits invisible disabled symbols, such as INHERIT when
            // kernel PI is disabled. Absence only satisfies a disabled boolean.
            let actual = effective.get(key.as_str()).copied().unwrap_or("n");
            if actual != expected {
                return Err(format!(
                    "unresolved NXRS resource-mutex policy {}: {} (effective {actual})",
                    self.name(),
                    line(&key, &expected),
                ));
            }
        }
        Ok(())
    }

    pub fn evidence(&self) -> Value {
        json!({
            "version":1,
            "mode":self.name(),
            "source":if self.control.is_some() {"qualification-control"} else {"default"},
            "reason":self.reason,
            "default_pthread_mutex_protocol":if self.control.is_none() {"inherit"} else {"none"},
            "priority_inheritance":self.inheritance(),
            "holder_tracking":if self.inheritance() {"embedded-single-holder"} else {"disabled"},
            "kconfig":self.values(),
        })
    }
}

/// Resolved selection for dry-run/deployment evidence. Effective Kconfig is
/// separately checked during inspection, preparation and the final build.
pub fn evidence(app: &App, options: &Options) -> Result<Value> {
    Ok(MutexPolicy::resolve(app, options)?.evidence())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(qualification: bool) -> (crate::TempDir, App) {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let path = temp.0.join("Cargo.toml");
        std::fs::write(&path, format!(
            "[package]\nname='fixture'\nversion='0.1.0'\n[package.metadata.nxrs]\nqualification-only={qualification}\n"
        )).unwrap();
        let app = App::load(&path, None).unwrap();
        (temp, app)
    }

    #[test]
    fn defaults_and_named_controls_have_distinct_exact_contracts() {
        let (_temp, app) = entry(true);
        for (control, mode, pi, protocol) in [
            (None, "inherit", true, "inherit"),
            (
                Some(QualificationMutexPolicy::NoInherit),
                "no-inherit",
                true,
                "none",
            ),
            (
                Some(QualificationMutexPolicy::KernelPiDisabled),
                "kernel-pi-disabled",
                false,
                "none",
            ),
        ] {
            let options = Options {
                qualification_mutex_policy: control,
                qualification_mutex_reason: control
                    .map(|_| "controlled scheduler comparison".into()),
                ..Options::default()
            };
            let policy = MutexPolicy::resolve(&app, &options).unwrap();
            let evidence = policy.evidence();
            assert_eq!(evidence["mode"], mode);
            assert_eq!(evidence["priority_inheritance"], pi);
            assert_eq!(evidence["default_pthread_mutex_protocol"], protocol);
            assert_eq!(
                policy
                    .values()
                    .get("SEM_PREALLOCHOLDERS")
                    .map(String::as_str),
                pi.then_some("0")
            );
            let mut config = policy
                .values()
                .iter()
                .map(|(key, value)| line(key, value) + "\n")
                .collect::<String>();
            policy.validate_config(&config).unwrap();
            config = config.replace(
                "# CONFIG_PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT is not set\n",
                "",
            );
            policy.validate_config(&config).unwrap();
            let broken = if pi {
                config.replace(
                    "CONFIG_SEM_PREALLOCHOLDERS=0",
                    "CONFIG_SEM_PREALLOCHOLDERS=8",
                )
            } else {
                config.replace(
                    "# CONFIG_PRIORITY_INHERITANCE is not set",
                    "CONFIG_PRIORITY_INHERITANCE=y",
                )
            };
            assert!(policy.validate_config(&broken).is_err());
        }
    }

    #[test]
    fn controls_require_fixture_metadata_reason_and_nuttx_backend() {
        let (_temp, app) = entry(false);
        let options = Options {
            qualification_mutex_policy: Some(QualificationMutexPolicy::NoInherit),
            qualification_mutex_reason: Some("negative control".into()),
            ..Options::default()
        };
        assert!(MutexPolicy::resolve(&app, &options)
            .unwrap_err()
            .contains("qualification-only"));
        assert!(options
            .validate_for_backend("linux")
            .unwrap_err()
            .contains("NuttX profile"));
        for reason in [None, Some(""), Some("  "), Some("two\nlines")] {
            let mut options = options.clone();
            options.qualification_mutex_reason = reason.map(str::to_owned);
            assert!(options.validate().is_err());
        }
        let mut reason_only = options.clone();
        reason_only.qualification_mutex_policy = None;
        assert!(reason_only.validate().is_err());
        for invalid in ["inherit", "off", "none", ""] {
            assert!(invalid.parse::<QualificationMutexPolicy>().is_err());
        }
    }

    #[test]
    fn legacy_defaults_requirements_and_forbidden_patterns_cannot_hide_a_conflict() {
        let (_temp, app) = entry(false);
        let policy = MutexPolicy::resolve(&app, &Options::default()).unwrap();
        for settings in [
            json!({"disable":["PRIORITY_INHERITANCE"]}),
            json!({"enable":["PTHREAD_MUTEX_DEFAULT_PRIO_NONE"]}),
            json!({"set":["SEM_PREALLOCHOLDERS=8"]}),
            json!({"require":["# CONFIG_PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT is not set"]}),
            json!({"forbid-regex":"^CONFIG_PRIORITY_INHERITANCE=y$"}),
        ] {
            let profile = Profile::from_data(json!({"kconfig":settings}), vec![]);
            assert!(policy
                .check_profile(&profile)
                .unwrap_err()
                .contains("resource-mutex policy"));
        }
        let profile = Profile::from_data(
            json!({"kconfig":{
                "enable":["PRIORITY_INHERITANCE"], "set":["SEM_PREALLOCHOLDERS=0"],
                "require":["CONFIG_PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT=y"],
                "forbid-regex":"^CONFIG_SMP=y$"
            }}),
            vec![],
        );
        policy.check_profile(&profile).unwrap();
    }

    #[test]
    fn all_native_policy_properties_must_survive_resolution() {
        let (_temp, app) = entry(false);
        let policy = MutexPolicy::resolve(&app, &Options::default()).unwrap();
        let values = policy.values();
        let valid = values
            .iter()
            .map(|(key, value)| line(key, value) + "\n")
            .collect::<String>();
        policy.validate_config(&valid).unwrap();
        for (key, value) in values {
            let old = line(&key, &value);
            let new = line(&key, if value == "y" { "n" } else { "y" });
            assert!(
                policy.validate_config(&valid.replace(&old, &new)).is_err(),
                "{key}"
            );
            if value != "n" {
                assert!(
                    policy
                        .validate_config(&valid.replace(&(old + "\n"), ""))
                        .is_err(),
                    "{key}"
                );
            }
        }
    }
}
