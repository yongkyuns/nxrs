//! Application descriptors are platform-free; this host controller supplies build wiring.
use crate::{
    deployment,
    profile::{manifest, Profile},
    run,
    target::Target,
    write_json, Result,
};
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
#[derive(Debug)]
pub struct App {
    pub manifest: PathBuf,
    pub package: String,
    pub bin: String,
    pub command: String,
    pub priority: u32,
    pub stack: u32,
}
impl App {
    pub fn load(path: &Path, bin: Option<&str>) -> Result<Self> {
        let path = path.canonicalize().map_err(|e| e.to_string())?;
        let data = manifest(&path)?;
        let package = data["package"]["name"]
            .as_str()
            .ok_or("missing package name")?
            .to_owned();
        let meta = data
            .pointer("/package/metadata/nxrs/firmware")
            .unwrap_or(&Value::Null);
        if !meta.is_null()
            && !meta.as_object().is_some_and(|fields| {
                fields
                    .keys()
                    .all(|key| ["bin", "command", "priority", "stack-size"].contains(&key.as_str()))
            })
        {
            return Err(
                "firmware metadata must contain only bin, command, priority and stack-size".into(),
            );
        }
        let text = |key: &str| -> Result<Option<&str>> {
            meta.get(key)
                .map(|value| {
                    value
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .ok_or_else(|| format!("firmware {key} must be a nonempty string"))
                })
                .transpose()
        };
        let declared_bin = text("bin")?;
        let declared_command = text("command")?;
        let bin = bin
            .or(declared_bin)
            .or_else(|| {
                data["bin"]
                    .as_array()
                    .filter(|b| b.len() == 1)
                    .and_then(|b| b[0]["name"].as_str())
            })
            .unwrap_or(&package)
            .to_owned();
        let command = declared_command
            .map(str::to_owned)
            .unwrap_or_else(|| bin.replace('-', "_"));
        let number = |key: &str, default| match meta.get(key) {
            None => Ok(default),
            Some(v) => v
                .as_u64()
                .filter(|n| *n > 0 && *n <= u32::MAX as u64)
                .map(|n| n as u32)
                .ok_or_else(|| format!("invalid firmware {key}")),
        };
        if !name(&package) || !name(&bin) || !name(&command) {
            return Err("invalid application build identity".into());
        }
        Ok(Self {
            manifest: path,
            package,
            bin,
            command,
            priority: number("priority", 100)?,
            stack: number("stack-size", 32768)?,
        })
    }
}
pub fn name(value: &str) -> bool {
    value
        .as_bytes()
        .first()
        .is_some_and(u8::is_ascii_alphanumeric)
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}

#[cfg(test)]
mod descriptor_tests {
    use super::*;

    #[test]
    fn external_entry_metadata_rejects_mistyped_fields_instead_of_defaulting() {
        let temporary = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let manifest = temporary.0.join("Cargo.toml");
        let prefix = "[package]\nname=\"external-entry\"\nversion=\"0.0.0\"\n[package.metadata.nxrs.firmware]\n";
        for fields in [
            "bin=17",
            "command=false",
            "priority=\"100\"",
            "stack_size=4096",
            "stack-size=0",
        ] {
            fs::write(&manifest, format!("{prefix}{fields}\n")).unwrap();
            assert!(App::load(&manifest, None).is_err(), "{fields}");
        }
        fs::write(&manifest,format!("{prefix}bin=\"product\"\ncommand=\"start_product\"\npriority=80\nstack-size=16384\n")).unwrap();
        let app = App::load(&manifest, None).unwrap();
        assert_eq!(
            (
                app.bin.as_str(),
                app.command.as_str(),
                app.priority,
                app.stack
            ),
            ("product", "start_product", 80, 16384)
        );
    }
}

#[derive(Debug, Default)]
pub struct Options {
    pub clean: bool,
    pub features: Vec<String>,
    pub no_default_features: bool,
    pub helpers: Vec<PathBuf>,
    pub enable: Vec<String>,
    pub disable: Vec<String>,
    pub abi: Option<String>,
    pub size: bool,
    pub trace: bool,
    pub immediate: bool,
}
impl Options {
    pub fn validate(&self) -> Result<()> {
        for symbol in self.enable.iter().chain(&self.disable) {
            if !symbol
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_uppercase)
                || !symbol
                    .bytes()
                    .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
            {
                return Err(format!("Invalid Kconfig symbol: {symbol}"));
            }
        }
        if let Some(symbol) = self.enable.iter().find(|s| self.disable.contains(s)) {
            return Err(format!("Conflicting Kconfig request: {symbol}"));
        }
        for feature in &self.features {
            if feature
                .split('/')
                .next_back()
                .is_some_and(|s| ["person-model", "pico-model"].contains(&s))
            {
                return Err(
                    "model dependencies follow the profile; remove manual model features".into(),
                );
            }
            // Cargo owns feature-name validation. Only reject malformed CLI values
            // here; valid additive features include '+' and Unicode identifiers.
            if feature.is_empty()
                || feature.starts_with('-')
                || feature
                    .chars()
                    .any(|c| c.is_whitespace() || c == ',' || c == '\0')
            {
                return Err("invalid Cargo feature".into());
            }
        }
        if self
            .abi
            .as_deref()
            .is_some_and(|s| !["active", "minimal"].contains(&s))
        {
            return Err("invalid ABI profile".into());
        }
        if self.immediate && !self.trace {
            return Err("immediate abort requires trace-only backtrace".into());
        }
        Ok(())
    }
    pub fn validate_for_backend(&self, os: &str) -> Result<()> {
        self.validate()?;
        if os != "nuttx"
            && (!self.helpers.is_empty()
                || !self.enable.is_empty()
                || !self.disable.is_empty()
                || self.abi.is_some()
                || self.size
                || self.trace
                || self.immediate)
        {
            return Err("NuttX qualification switches require a NuttX profile".into());
        }
        Ok(())
    }

    /// Forward dependency selection to Cargo with optional profile-required features.
    pub fn configure_cargo(&self, command: &mut Command, required: &[String]) {
        let mut features = self.features.clone();
        features.extend_from_slice(required);
        if !features.is_empty() {
            command.args(["--features", &features.join(",")]);
        }
        if self.no_default_features {
            command.arg("--no-default-features");
        }
    }
}
pub fn selection(root: &Path, platform: Target, name: Option<&str>) -> Result<Profile> {
    let p = Profile::resolve(root, name.unwrap_or(platform.default_profile()))?;
    let board = p.data["board"].as_str().unwrap_or("");
    let matches = match platform {
        Target::Wroom => {
            board.starts_with("esp32s3-devkit:") || board.starts_with("esp32s3-devkit")
        }
        Target::CoreS3 => board.starts_with("esp32s3-m5-cores3:"),
        Target::Pico2W => board.starts_with("raspberrypi-pico-2:"),
        Target::Linux | Target::Macos | Target::Windows => {
            p.validate_target(platform.name()).is_ok()
        }
        Target::Web => p.validate_target("emscripten").is_ok(),
    };
    if !matches || (platform == Target::Wroom && p.name().starts_with("m5stack-cores3")) {
        return Err("profile belongs to another platform family".into());
    }
    Ok(p)
}
pub fn prepare_output(root: &Path, out: &Path) -> Result<PathBuf> {
    let absolute = if out.is_absolute() {
        out.to_path_buf()
    } else {
        root.join(out)
    };
    if absolute
        .components()
        .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err("output must not contain parent traversal".into());
    }
    let mut ancestor = absolute.as_path();
    while !ancestor.exists() {
        ancestor = ancestor.parent().ok_or("output parent missing")?;
    }
    let canonical = ancestor.canonicalize().map_err(|e| e.to_string())?;
    let resolved = canonical.join(absolute.strip_prefix(ancestor).map_err(|e| e.to_string())?);
    let repo = root.canonicalize().map_err(|e| e.to_string())?;
    if repo.starts_with(&resolved)
        || resolved.starts_with(repo.join("external"))
        || resolved.starts_with(repo.join("platform"))
        || resolved.starts_with(repo.join("hal"))
        || resolved.starts_with(repo.join("app"))
        || resolved.starts_with(repo.join(".git"))
    {
        return Err("output overlaps source or repository metadata".into());
    }
    let marker = resolved.join(".nxrs-build.json");
    if resolved.exists() && !marker.is_file() {
        // Qualification drivers may open their log before starting the builder.
        let logs_only = fs::read_dir(&resolved)
            .map_err(|e| e.to_string())?
            .try_fold(true, |clean, entry| -> Result<bool> {
                let entry = entry.map_err(|e| e.to_string())?;
                Ok(clean
                    && entry.file_type().map_err(|e| e.to_string())?.is_file()
                    && entry.path().extension().is_some_and(|s| s == "log"))
            })?;
        if !logs_only {
            return Err(
                "existing output is not an owned build directory; choose a fresh --out".into(),
            );
        }
    }
    if marker.symlink_metadata().is_ok_and(|m| m.is_symlink()) {
        return Err("build ownership marker must not be a symlink".into());
    }
    fs::create_dir_all(&resolved).map_err(|e| e.to_string())?;
    if marker.exists() && crate::read_json(&marker)? != json!({"workspace":repo}) {
        return Err("output belongs to another workspace".into());
    }
    write_json(&marker, &json!({"workspace":repo}))?;
    Ok(resolved)
}
pub fn host(
    root: &Path,
    app: &App,
    profile: &Profile,
    out: &Path,
    options: &Options,
) -> Result<()> {
    let os = std::env::consts::OS;
    let mut evidence = deployment::validate(root, &app.manifest, profile, os)?;
    options.validate_for_backend(os)?;
    let out = prepare_output(root, out)?;
    if options.clean {
        crate::remove_dir(&out.join("cargo"))?;
    }
    crate::remove_file(&out.join("provider-selection.json"))?;
    let mut command = Command::new("cargo");
    command
        .current_dir(root)
        .args(["build", "--locked", "--release", "--manifest-path"])
        .arg(&app.manifest)
        .args(["--bin", &app.bin])
        .env("NXRS_PLATFORM", profile.name())
        .env("NXRS_SDK_ROOT", root)
        .env("CARGO_TARGET_DIR", out.join("cargo"));
    options.configure_cargo(&mut command, &[]);
    let selection = crate::cargo_graph::resolve(&mut command, profile)?;
    evidence["dependency_resolution"] = selection.json();
    evidence["model_features"] = json!(selection.model_features);
    write_json(&out.join("provider-selection.json"), &evidence)?;
    run(&mut command)
}
pub fn std_source(messages: &Path, sysroot: &Path, rustc: &Path, out: &Path) -> Result<()> {
    let mut selected = vec![];
    for line in crate::read(messages)?.lines() {
        let message: Value =
            serde_json::from_str(line).map_err(|e| format!("invalid Cargo message: {e}"))?;
        if message["reason"] == "compiler-artifact"
            && message["target"]["name"] == "std"
            && message["target"]["crate_types"]
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v == "rlib"))
        {
            selected.push(message);
        }
    }
    if selected.len() != 1 {
        return Err(format!(
            "expected one std artifact, found {}",
            selected.len()
        ));
    }
    let actual = Path::new(
        selected[0]["target"]["src_path"]
            .as_str()
            .ok_or("missing std source")?,
    )
    .canonicalize()
    .map_err(|e| e.to_string())?;
    let expected = sysroot
        .join("lib/rustlib/src/rust/library/std/src/lib.rs")
        .canonicalize()
        .map_err(|e| e.to_string())?;
    write_json(
        &out.join("std-source.json"),
        &json!({"std_source":actual,"expected_source":expected,"rustc":rustc,"selected_source_matches":actual==expected}),
    )?;
    if actual != expected {
        return Err("Cargo selected the wrong standard-library source".into());
    }
    Ok(())
}
/// Stream Cargo JSON into evidence while keeping human diagnostics visible.
pub fn cargo_messages(command: &mut Command, path: &Path) -> Result<()> {
    use std::io::{BufRead, Write};
    use std::process::Stdio;
    let mut child = command
        .stdout(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let result: Result<()> = (|| {
        let mut log = fs::File::create(path).map_err(|e| e.to_string())?;
        for line in std::io::BufReader::new(child.stdout.take().unwrap()).lines() {
            let line = line.map_err(|e| e.to_string())?;
            writeln!(log, "{line}").map_err(|e| e.to_string())?;
            if let Ok(message) = serde_json::from_str::<Value>(&line) {
                if let Some(rendered) = message.pointer("/message/rendered").and_then(Value::as_str)
                {
                    eprint!("{rendered}");
                }
            }
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = child.kill();
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    result?;
    if !status.success() {
        return Err(format!("Cargo build failed: {status}"));
    }
    Ok(())
}
