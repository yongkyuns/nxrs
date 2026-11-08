//! Optional provider-owned C/Objective-C ABI compilation; invoked on the build host.
use crate::{output, profile::manifest, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    ffi::{OsStr, OsString},
    fs,
    path::{Path, PathBuf},
    process::Command,
};

/// Explicit native-build inputs. Capturing a Cargo command does not change the
/// process environment; its additions/removals apply only to child processes.
/// Cargo's build-script feature spelling is deliberately not inverted: `a-b`
/// and `a_b` both project to `CARGO_FEATURE_A_B`.
#[derive(Clone)]
pub struct Context {
    environment: BTreeMap<OsString, OsString>,
    features: BTreeSet<String>,
}

impl Context {
    pub fn for_command(command: &Command, features: BTreeSet<String>) -> Result<Self> {
        let mut environment: BTreeMap<_, _> = env::vars_os().collect();
        for (key, value) in command.get_envs() {
            if let Some(value) = value {
                environment.insert(key.to_owned(), value.to_owned());
            } else {
                environment.remove(key);
            }
        }
        let features: BTreeSet<_> = features.iter().map(|name| feature_variable(name)).collect();
        environment.retain(|key, _| {
            !key.to_str()
                .is_some_and(|key| key.starts_with("CARGO_FEATURE_"))
        });
        for feature in &features {
            environment.insert(feature.into(), "1".into());
        }
        Ok(Self {
            environment,
            features,
        })
    }

    pub fn from_build_script() -> Result<Self> {
        let environment: BTreeMap<_, _> = env::vars_os().collect();
        let features = environment
            .keys()
            .filter_map(|name| name.to_str())
            .filter(|name| name.starts_with("CARGO_FEATURE_"))
            .map(str::to_owned)
            .collect();
        Ok(Self {
            environment,
            features,
        })
    }

    pub fn feature_environment(&self) -> &BTreeSet<String> {
        &self.features
    }

    pub(crate) fn value(&self, name: &str) -> Result<Option<String>> {
        self.environment
            .get(OsStr::new(name))
            .map(|value| {
                value
                    .to_str()
                    .map(str::to_owned)
                    .ok_or_else(|| format!("native input {name} must be UTF-8"))
            })
            .transpose()
    }

    fn configured_words(&self, name: &str, default: &str) -> Result<Vec<String>> {
        let result = words(self.value(name)?.as_deref().unwrap_or(default))?;
        if result.first().is_none_or(String::is_empty) {
            return Err(format!("{name} must name a command"));
        }
        Ok(result)
    }

    pub(crate) fn command(&self, words: &[String], directory: &Path) -> Result<Command> {
        let (program, args) = words.split_first().ok_or("empty native command")?;
        if program.is_empty() {
            return Err("empty native compiler/archive command".into());
        }
        let mut command = Command::new(program);
        command
            .args(args)
            .env_clear()
            .envs(&self.environment)
            .current_dir(directory);
        Ok(command)
    }

    pub(crate) fn environment_inputs(
        &self,
        names: &[&str],
    ) -> Result<BTreeMap<String, Option<String>>> {
        names
            .iter()
            .map(|name| Ok(((*name).to_owned(), self.value(name)?)))
            .collect()
    }

    fn program(&self, value: &str, directory: &Path) -> Result<PathBuf> {
        let path = Path::new(value);
        let candidate = if path.is_absolute() {
            path.to_owned()
        } else if path.components().count() > 1 {
            directory.join(path)
        } else {
            env::split_paths(
                self.environment
                    .get(OsStr::new("PATH"))
                    .map(OsString::as_os_str)
                    .unwrap_or_default(),
            )
            .map(|entry| {
                if entry.is_absolute() {
                    entry.join(path)
                } else {
                    directory.join(entry).join(path)
                }
            })
            .find(|path| path.is_file())
            .ok_or_else(|| format!("missing prerequisite {value}"))?
        };
        candidate
            .canonicalize()
            .map_err(|error| format!("native tool {}: {error}", candidate.display()))
    }

    /// Record the actual selected program, not a Cargo-modified PATH string.
    /// Also retain file/tool arguments used by ordinary compiler wrappers. This
    /// does not discover arbitrary wrapper-specific environment or file inputs.
    pub(crate) fn tool_identity(
        &self,
        words: &[String],
        directory: &Path,
        files: &mut Vec<PathBuf>,
    ) -> Result<Value> {
        let program = words.first().ok_or("empty native command")?;
        let selected = self.program(program, directory)?;
        let mut programs = vec![selected];
        for argument in words.iter().skip(1).filter(|word| !word.starts_with('-')) {
            if let Ok(path) = self.program(argument, directory) {
                if path.is_file() && !programs.contains(&path) {
                    programs.push(path);
                }
            }
        }
        let identities = programs
            .iter()
            .map(|path| Ok(json!({"path": path, "sha256": crate::hash(path)?})))
            .collect::<Result<Vec<_>>>()?;
        files.extend(programs);
        Ok(json!({"argv": words, "programs": identities}))
    }
}

fn feature_variable(name: &str) -> String {
    format!("CARGO_FEATURE_{}", name.replace('-', "_").to_uppercase())
}

/// A deterministic preparation snapshot. `files` also contains source/include
/// directories whose entry inventory must be tracked for additions/removals.
/// Only native-build environment inputs are retained; the captured execution
/// environment is private and is never serialized into a receipt.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct Inputs {
    pub files: Vec<PathBuf>,
    pub environment: BTreeMap<String, Option<String>>,
    pub identity: Value,
}

pub(crate) const COMPILER_ENVIRONMENT: &[&str] = &[
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "OBJC_INCLUDE_PATH",
    "LIBRARY_PATH",
    "COMPILER_PATH",
    "GCC_EXEC_PREFIX",
    "SOURCE_DATE_EPOCH",
    "DEVELOPER_DIR",
    "SDKROOT",
    "MACOSX_DEPLOYMENT_TARGET",
];
fn words(value: &str) -> Result<Vec<String>> {
    shlex::split(value).ok_or_else(|| format!("malformed command/flags: {value}"))
}
pub fn command(name: &str, default: &str) -> Result<Command> {
    let words = words(&env::var(name).unwrap_or_else(|_| default.into()))?;
    let (program, args) = words
        .split_first()
        .ok_or_else(|| format!("{name} must name a command"))?;
    let mut c = Command::new(program);
    c.args(args);
    Ok(c)
}
fn simple(s: &str, package: bool) -> bool {
    !s.is_empty()
        && (!package || s.as_bytes()[0].is_ascii_alphanumeric())
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || (package && ".+-".contains(c)))
}
fn strings(v: Option<&Value>, package: bool) -> Result<Vec<String>> {
    let Some(v) = v else { return Ok(vec![]) };
    let mut result = v
        .as_array()
        .ok_or("native metadata requires a string array")?
        .iter()
        .map(|v| {
            v.as_str()
                .filter(|s| simple(s, package))
                .map(str::to_owned)
                .ok_or("invalid native dependency name".to_owned())
        })
        .collect::<Result<Vec<_>>>()?;
    result.sort();
    result.dedup();
    Ok(result)
}
fn flags(text: &str, kind: &str) -> Result<(Vec<String>, Vec<String>)> {
    let words = words(text)?;
    let mut iter = words.iter();
    let (mut compile, mut link) = (vec![], vec![]);
    while let Some(word) = iter.next() {
        if kind == "cflags" {
            if ["-I", "-D", "-U", "-isystem", "-include"].contains(&word.as_str()) {
                compile.push(word.clone());
                compile.push(
                    iter.next()
                        .filter(|s| !s.is_empty())
                        .ok_or("missing compiler flag value")?
                        .clone(),
                );
            } else if word == "-pthread"
                || ["-I", "-D", "-U", "-f"]
                    .iter()
                    .any(|p| word.starts_with(p) && word.len() > p.len())
            {
                compile.push(word.clone())
            } else {
                return Err(format!("unrecognized pkg-config cflags flag: {word}"));
            }
        } else if word == "-pthread" {
            link.push("cargo:rustc-link-lib=pthread".into())
        } else {
            let (prefix, value) = if ["-L", "-l"].contains(&word.as_str()) {
                (
                    word.as_str(),
                    iter.next()
                        .filter(|s| !s.is_empty())
                        .ok_or("missing linker flag value")?
                        .as_str(),
                )
            } else if word.starts_with("-L") || word.starts_with("-l") {
                (&word[..2], &word[2..])
            } else {
                return Err(format!("unrecognized pkg-config libs flag: {word}"));
            };
            if value.is_empty() {
                return Err("missing linker flag value".into());
            }
            link.push(if prefix == "-L" {
                format!("cargo:rustc-link-search=native={value}")
            } else {
                format!("cargo:rustc-link-lib={value}")
            });
        }
    }
    Ok((compile, link))
}
pub(crate) fn tree(dir: &Path, result: &mut Vec<PathBuf>) -> Result<()> {
    fn visit(dir: &Path, seen: &mut BTreeSet<PathBuf>, result: &mut Vec<PathBuf>) -> Result<()> {
        if !dir.is_dir() || !seen.insert(dir.canonicalize().map_err(|e| e.to_string())?) {
            return Ok(());
        }
        for e in fs::read_dir(dir).map_err(|e| e.to_string())? {
            let p = e.map_err(|e| e.to_string())?.path();
            result.push(p.clone());
            if p.is_dir() {
                visit(&p, seen, result)?
            }
        }
        Ok(())
    }
    visit(dir, &mut BTreeSet::new(), result)
}

/// Inspect only enabled helpers, without compiling them. The controller records
/// this snapshot before native preparation and checks the compiler-discovered
/// header paths returned by `build_with_context` as additional inputs afterward.
pub fn inputs(capability: &Path, os: &str, target: &str, context: &Context) -> Result<Inputs> {
    let capability = &capability.canonicalize().map_err(|e| e.to_string())?;
    let source_dir = capability.join("src/platform").join(os).join("ffi");
    let mut watch = source_dir.clone();
    while !watch.is_dir() && watch != *capability {
        watch = watch.parent().ok_or("native source parent missing")?.into();
    }
    let mut paths = Vec::new();
    tree(&source_dir, &mut paths)?;
    paths.sort();
    let sources: Vec<_> = paths
        .iter()
        .filter(|path| {
            path.parent() == Some(&source_dir)
                && path.is_file()
                && path.extension().is_some_and(|e| e == "c" || e == "m")
        })
        .cloned()
        .collect();
    let mut result = Inputs {
        files: vec![capability.join("Cargo.toml"), watch],
        environment: BTreeMap::new(),
        identity: json!({"os": os, "target": target, "features": context.features,
            "sources": sources, "enabled": false}),
    };
    if sources.is_empty() {
        return Ok(result);
    }
    let m = manifest(&capability.join("Cargo.toml"))?;
    let native = m
        .pointer(&format!("/package/metadata/nxrs/platform/{os}"))
        .unwrap_or(&Value::Null);
    if !native.is_null() && !native.is_object() {
        return Err("native metadata must be a table".into());
    }
    let mut enabled = true;
    for feature in strings(native.get("required-features"), true)? {
        if m["features"].get(&feature).is_none() {
            return Err(format!(
                "native helper requires undeclared Cargo feature {feature}"
            ));
        }
        enabled &= context.features.contains(&feature_variable(&feature));
    }
    result.identity["native"] = native.clone();
    result.identity["enabled"] = json!(enabled);
    if !enabled {
        return Ok(result);
    }
    result.files.extend(paths.into_iter().filter(|path| {
        path.is_dir()
            || path
                .extension()
                .is_some_and(|e| ["c", "m", "h", "hpp"].iter().any(|suffix| e == *suffix))
    }));
    if os == "nuttx" {
        let nuttx = crate::native_nuttx::inputs(target, context)?;
        result.files.extend(nuttx.files);
        result.environment = nuttx.environment;
        result.identity["toolchain"] = nuttx.identity;
    } else {
        validate_host(os, target, &sources)?;
        let mut include_roots = Vec::new();
        result.environment = context.environment_inputs(COMPILER_ENVIRONMENT)?;
        let (cc, ar) = if os == "linux" {
            result.environment.extend(context.environment_inputs(&[
                "CC",
                "AR",
                "PKG_CONFIG",
                "PKG_CONFIG_PATH",
                "PKG_CONFIG_LIBDIR",
                "PKG_CONFIG_SYSROOT_DIR",
            ])?);
            let packages = strings(native.get("pkg-config"), true)?;
            if !packages.is_empty() {
                let program = context.configured_words("PKG_CONFIG", "pkg-config")?;
                let tool = context.tool_identity(&program, capability, &mut result.files)?;
                let mut output_flags = BTreeMap::new();
                for kind in ["cflags", "libs"] {
                    let text = output(
                        context
                            .command(&program, capability)?
                            .arg(format!("--{kind}"))
                            .args(&packages),
                    )?;
                    let (compile, link) = flags(&text, kind)?;
                    output_flags.insert(kind, json!({"compile": compile, "link": link}));
                    include_paths(&compile, capability, &mut include_roots);
                }
                result.identity["pkg-config"] = json!({"tool": tool, "flags": output_flags});
            }
            (
                context.configured_words("CC", "cc")?,
                context.configured_words("AR", "ar")?,
            )
        } else {
            // xcrun itself is stable across SDK/selected-developer changes.
            // Capture its actual clang selection and SDK path as well.
            let xcrun = vec!["xcrun".into(), "--sdk".into(), "macosx".into()];
            let clang = output(
                context
                    .command(&xcrun, capability)?
                    .args(["--find", "clang"]),
            )?;
            let sdk = output(context.command(&xcrun, capability)?.arg("--show-sdk-path"))?;
            result.identity["xcrun"] =
                context.tool_identity(&xcrun, capability, &mut result.files)?;
            result.identity["sdk"] = json!(sdk.trim());
            (vec![clang.trim().to_owned()], vec!["ar".into()])
        };
        result.identity["compiler"] = context.tool_identity(&cc, capability, &mut result.files)?;
        result.identity["archiver"] = context.tool_identity(&ar, capability, &mut result.files)?;
        include_paths(&cc, capability, &mut include_roots);
        for name in [
            "CPATH",
            "C_INCLUDE_PATH",
            "CPLUS_INCLUDE_PATH",
            "OBJC_INCLUDE_PATH",
        ] {
            if let Some(value) = context.value(name)? {
                for path in env::split_paths(&value) {
                    let path = if path.is_absolute() {
                        path
                    } else {
                        capability.join(path)
                    };
                    if path.is_dir() {
                        include_roots.push(path);
                    }
                }
            }
        }
        for path in include_roots {
            tree(&path, &mut result.files)?;
            result.files.push(path);
        }
    }
    result.files.sort();
    result.files.dedup();
    Ok(result)
}

pub(crate) fn include_paths(flags: &[String], directory: &Path, paths: &mut Vec<PathBuf>) {
    let mut flags = flags.iter();
    while let Some(flag) = flags.next() {
        let value =
            if ["-I", "-isystem", "-include", "-iquote", "-idirafter"].contains(&flag.as_str()) {
                flags.next().map(String::as_str)
            } else {
                flag.strip_prefix("-I").filter(|value| !value.is_empty())
            };
        if let Some(value) = value {
            let path = PathBuf::from(value);
            let path = if path.is_absolute() {
                path
            } else {
                directory.join(path)
            };
            if path.exists() {
                paths.push(path);
            }
        }
    }
}

fn validate_host(os: &str, target: &str, sources: &[PathBuf]) -> Result<()> {
    if !["linux", "macos"].contains(&os) {
        return Err(format!("native sources for {os} are not supported yet"));
    }
    if os == "macos" && !target.ends_with("-apple-darwin") {
        return Err("native macOS sources require an Apple Darwin target".into());
    }
    if env::consts::OS != os {
        return Err(format!("native {os} sources require a {os} host"));
    }
    if os == "linux"
        && (!target.starts_with(&format!("{}-", env::consts::ARCH))
            || !target.ends_with("-linux-gnu")
            || sources
                .iter()
                .any(|p| p.extension().is_some_and(|e| e != "c")))
    {
        return Err(
            "native Linux sources require matching host architecture and a GNU target".into(),
        );
    }
    Ok(())
}

pub fn build(capability: &Path, os: &str, target: &str, out: &Path) -> Result<Vec<String>> {
    build_with_context(capability, os, target, out, &Context::from_build_script()?)
}

/// Build a consumed package's helper with Cargo's selected target features and
/// the controller's explicit environment. No process-global mutation is needed.
pub fn build_with_context(
    capability: &Path,
    os: &str,
    target: &str,
    out: &Path,
    context: &Context,
) -> Result<Vec<String>> {
    let capability = &capability.canonicalize().map_err(|e| e.to_string())?;
    let out = &std::path::absolute(out).map_err(|e| e.to_string())?;
    let source_dir = capability.join("src/platform").join(os).join("ffi");
    let mut watch = source_dir.clone();
    while !watch.is_dir() && watch != *capability {
        watch = watch.parent().ok_or("native source parent missing")?.into()
    }
    let mut inputs = vec![
        format!(
            "cargo:rerun-if-changed={}",
            capability.join("Cargo.toml").display()
        ),
        format!("cargo:rerun-if-changed={}", watch.display()),
    ];
    let mut paths = vec![];
    tree(&source_dir, &mut paths)?;
    paths.sort();
    let sources: Vec<_> = paths
        .iter()
        .filter(|p| {
            p.parent() == Some(&source_dir)
                && p.is_file()
                && p.extension().is_some_and(|e| e == "c" || e == "m")
        })
        .collect();
    if sources.is_empty() {
        return Ok(inputs);
    }
    let m = manifest(&capability.join("Cargo.toml"))?;
    let package = m["package"]["name"]
        .as_str()
        .ok_or("missing package name")?;
    // Cargo sources may live in renamed or versioned directories. The archive
    // belongs to the package, independently of its checkout location.
    let suffix = package
        .strip_prefix("nxrs-")
        .unwrap_or(package)
        .replace('-', "_");
    if !simple(&suffix, false) {
        return Err("unsafe native archive name".into());
    }
    let native = m
        .pointer(&format!("/package/metadata/nxrs/platform/{os}"))
        .unwrap_or(&Value::Null);
    if !native.is_null() && !native.is_object() {
        return Err("native metadata must be a table".into());
    }
    let mut enabled = true;
    for feature in strings(native.get("required-features"), true)? {
        if m["features"].get(&feature).is_none() {
            return Err(format!(
                "native helper requires undeclared Cargo feature {feature}"
            ));
        }
        let variable = feature_variable(&feature);
        inputs.push(format!("cargo:rerun-if-env-changed={variable}"));
        enabled &= context.features.contains(&variable);
    }
    if !enabled {
        return Ok(inputs);
    }
    if os == "nuttx" {
        return crate::native_nuttx::build(
            capability, &suffix, target, out, &sources, inputs, context,
        );
    }
    if !["linux", "macos"].contains(&os) {
        return Err(format!("native sources for {os} are not supported yet"));
    }
    if os == "macos" && !target.ends_with("-apple-darwin") {
        return Err("native macOS sources require an Apple Darwin target".into());
    }
    if env::consts::OS != os {
        return Err(format!("native {os} sources require a {os} host"));
    }
    if os == "linux"
        && (!target.starts_with(&format!("{}-", env::consts::ARCH))
            || !target.ends_with("-linux-gnu")
            || sources
                .iter()
                .any(|p| p.extension().is_some_and(|e| e != "c")))
    {
        return Err(
            "native Linux sources require matching host architecture and a GNU target".into(),
        );
    }
    for p in &paths {
        if p.is_dir()
            || p.extension()
                .is_some_and(|e| ["c", "m", "h", "hpp"].iter().any(|s| e == *s))
        {
            inputs.push(format!("cargo:rerun-if-changed={}", p.display()))
        }
    }
    let frameworks = if os == "macos" {
        strings(native.get("frameworks"), false)?
    } else {
        vec![]
    };
    let mut compile = vec![];
    let mut link = vec![];
    if os == "linux" {
        for name in [
            "PKG_CONFIG",
            "PKG_CONFIG_PATH",
            "PKG_CONFIG_LIBDIR",
            "PKG_CONFIG_SYSROOT_DIR",
            "CC",
            "AR",
        ] {
            inputs.push(format!("cargo:rerun-if-env-changed={name}"))
        }
        let packages = strings(native.get("pkg-config"), true)?;
        if !packages.is_empty() {
            for kind in ["cflags", "libs"] {
                let text = output(
                    context
                        .command(
                            &context.configured_words("PKG_CONFIG", "pkg-config")?,
                            capability,
                        )?
                        .arg(format!("--{kind}"))
                        .args(&packages),
                )?;
                let (c, l) = flags(&text, kind)?;
                compile.extend(c);
                link.extend(l)
            }
        }
    }
    fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let fresh = crate::TempDir::new(out)?;
    let mut objects = vec![];
    for (i, source) in sources.iter().enumerate() {
        let object = fresh.0.join(format!("{suffix}_{i:04}.o"));
        let dependency_file = fresh.0.join(format!("{suffix}_{i:04}.d"));
        let mut c = if os == "macos" {
            let mut c = context.command(&["xcrun".into()], capability)?;
            c.args(["--sdk", "macosx", "clang", "-target", target]);
            if source.extension().is_some_and(|e| e == "m") {
                c.arg("-fobjc-arc");
            }
            c
        } else {
            let mut c = context.command(&context.configured_words("CC", "cc")?, capability)?;
            c.args(&compile);
            c
        };
        c.args(["-Wall", "-Wextra", "-Werror", "-I"])
            .arg(&source_dir)
            .arg("-c")
            .arg(source)
            .arg("-o")
            .arg(&object)
            .args(["-MD", "-MT", "nxrs_native", "-MF"])
            .arg(&dependency_file);
        output(&mut c)?;
        for dependency in crate::native_nuttx::dependencies(&crate::read(&dependency_file)?)? {
            let path = if dependency.is_absolute() {
                dependency
            } else {
                capability.join(dependency)
            };
            crate::native_nuttx::input(&path, &mut inputs)?;
        }
        objects.push(object);
    }
    let name = format!("lib{suffix}_native.a");
    let archive = fresh.0.join(&name);
    let result = (|| {
        let mut c = if os == "linux" {
            context.command(&context.configured_words("AR", "ar")?, capability)?
        } else {
            context.command(&["ar".into()], capability)?
        };
        output(c.arg("crs").arg(&archive).args(&objects))?;
        if !archive.is_file() {
            return Err("native archive command produced no archive".into());
        }
        fs::rename(&archive, out.join(&name)).map_err(|e| e.to_string())
    })();
    result?;
    let mut directives = vec![
        format!("cargo:rustc-link-search=native={}", out.display()),
        format!("cargo:rustc-link-lib=static={suffix}_native"),
    ];
    directives.extend(link);
    directives.extend(
        frameworks
            .iter()
            .map(|f| format!("cargo:rustc-link-lib=framework={f}")),
    );
    if os == "macos" {
        inputs.extend(
            ["DEVELOPER_DIR", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET"]
                .iter()
                .map(|s| format!("cargo:rerun-if-env-changed={s}")),
        )
    }
    inputs.extend(
        COMPILER_ENVIRONMENT
            .iter()
            .map(|name| format!("cargo:rerun-if-env-changed={name}")),
    );
    inputs.push("cargo:rerun-if-env-changed=PATH".into());
    inputs.sort();
    inputs.dedup();
    directives.extend(inputs);
    Ok(directives)
}

/// Compile explicitly listed sources for a NuttX qualification fixture using
/// the build frontend's pinned target headers and compiler context.
pub fn build_nuttx_sources(
    source_directory: &Path,
    suffix: &str,
    target: &str,
    out: &Path,
    sources: &[&PathBuf],
) -> Result<Vec<String>> {
    crate::native_nuttx::build_sources(source_directory, suffix, target, out, sources)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disabled_helpers_need_no_native_context_or_toolchain() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let cap = temp.0.join("capability");
        let ffi = cap.join("src/platform/nuttx/ffi");
        fs::create_dir_all(&ffi).unwrap();
        fs::write(
            cap.join("Cargo.toml"),
            "[package]\nname=\"nxrs-fixture\"\n[features]\nnative-bridge=[]\n\
             [package.metadata.nxrs.platform.nuttx]\nrequired-features=[\"native-bridge\"]\n",
        )
        .unwrap();
        fs::write(ffi.join("value.c"), "#error disabled helper compiled\n").unwrap();
        let mut command = Command::new("cargo");
        command
            .env("NXRS_NUTTX_NATIVE_CONFIG", temp.0.join("missing.json"))
            .env("CC", "missing-native-compiler")
            .env("AR", "missing-native-archiver")
            .env("CARGO_FEATURE_NATIVE_BRIDGE", "1");
        // The controller's actual Cargo feature set overrides unrelated inherited
        // or manually injected build-script feature variables.
        let context = Context::for_command(&command, BTreeSet::new()).unwrap();
        let snapshot = inputs(&cap, "nuttx", "fixture-nuttx", &context).unwrap();
        assert_eq!(snapshot.identity["enabled"], false);
        assert!(snapshot.environment.is_empty());
        let out = temp.0.join("out");
        let directives =
            build_with_context(&cap, "nuttx", "fixture-nuttx", &out, &context).unwrap();
        assert!(!out.exists());
        assert!(
            directives.contains(&"cargo:rerun-if-env-changed=CARGO_FEATURE_NATIVE_BRIDGE".into())
        );
        let context =
            Context::for_command(&command, BTreeSet::from(["native-bridge".into()])).unwrap();
        assert_eq!(
            context.feature_environment(),
            &BTreeSet::from(["CARGO_FEATURE_NATIVE_BRIDGE".into()])
        );
        assert!(inputs(&cap, "nuttx", "fixture-nuttx", &context).is_err());
    }

    #[test]
    fn source_less_package_does_not_probe_native_tools() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        fs::write(
            temp.0.join("Cargo.toml"),
            "[package]\nname=\"nxrs-fixture\"\n",
        )
        .unwrap();
        let mut command = Command::new("cargo");
        command.env("PATH", "").env("CC", "missing-compiler");
        let context = Context::for_command(&command, BTreeSet::new()).unwrap();
        let result = inputs(&temp.0, "nuttx", "unconfigured-target", &context).unwrap();
        assert_eq!(result.identity["enabled"], false);
        assert!(result.environment.is_empty());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn controller_context_uses_explicit_features_environment_and_transitive_headers() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let cap = temp.0.join("capability");
        let ffi = cap.join("src/platform/linux/ffi");
        let include = temp.0.join("first include");
        let fallback = temp.0.join("external headers #$");
        fs::create_dir_all(&ffi).unwrap();
        fs::create_dir_all(include.join("sub")).unwrap();
        fs::create_dir_all(fallback.join("sub")).unwrap();
        fs::write(
            cap.join("Cargo.toml"),
            "[package]\nname=\"nxrs-fixture\"\n[features]\nnative-bridge=[]\n\
             [package.metadata.nxrs.platform.linux]\nrequired-features=[\"native-bridge\"]\n",
        )
        .unwrap();
        let header = fallback.join("sub/value.h");
        fs::write(&header, "#define HEADER_VALUE 3\n").unwrap();
        fs::write(
            ffi.join("value.c"),
            "#include <sub/value.h>\nint context_value(void) { return FROM_CONTEXT + HEADER_VALUE; }\n",
        )
        .unwrap();
        let before = env::var_os("CC");
        let mut command = Command::new("cargo");
        command
            .env("CC", "cc -DFROM_CONTEXT=17")
            .env("AR", "ar")
            .env("CPATH", env::join_paths([&include, &fallback]).unwrap())
            .env_remove("PKG_CONFIG");
        let context =
            Context::for_command(&command, BTreeSet::from(["native-bridge".into()])).unwrap();
        let rustc = crate::output(Command::new("rustc").arg("-vV")).unwrap();
        let target = rustc
            .lines()
            .find_map(|line| line.strip_prefix("host: "))
            .unwrap();
        let snapshot = inputs(&cap, "linux", target, &context).unwrap();
        assert_eq!(snapshot.identity["enabled"], true);
        assert_eq!(
            snapshot.environment["CC"].as_deref(),
            Some("cc -DFROM_CONTEXT=17")
        );
        assert_eq!(snapshot.environment["PKG_CONFIG"], None);
        assert!(snapshot.files.contains(&include));
        assert!(snapshot.files.contains(&include.join("sub")));
        assert!(!snapshot.environment.contains_key("PATH"));
        let out = temp.0.join("out");
        let directives = build_with_context(&cap, "linux", target, &out, &context).unwrap();
        assert!(directives.contains(&format!("cargo:rerun-if-changed={}", header.display())));
        assert_eq!(env::var_os("CC"), before);
        let main = temp.0.join("main.c");
        let executable = temp.0.join("witness");
        fs::write(
            &main,
            "int context_value(void); int main(void) { return context_value() == 20 ? 0 : 1; }\n",
        )
        .unwrap();
        output(
            Command::new("cc")
                .arg(&main)
                .arg(out.join("libfixture_native.a"))
                .arg("-o")
                .arg(&executable),
        )
        .unwrap();
        assert!(Command::new(&executable).status().unwrap().success());
        // A new header in a nested, higher-priority include directory changes
        // resolution while the previously used header remains byte-identical.
        let shadow = include.join("sub/value.h");
        fs::write(&shadow, "#define HEADER_VALUE 7\n").unwrap();
        let changed = inputs(&cap, "linux", target, &context).unwrap();
        assert_ne!(changed, snapshot);
        assert!(changed.files.contains(&shadow));
        let directives = build_with_context(&cap, "linux", target, &out, &context).unwrap();
        assert!(directives.contains(&format!("cargo:rerun-if-changed={}", shadow.display())));
        output(
            Command::new("cc")
                .arg(&main)
                .arg(out.join("libfixture_native.a"))
                .arg("-o")
                .arg(&executable),
        )
        .unwrap();
        assert!(!Command::new(executable).status().unwrap().success());
        let mut disabled = Command::new("cargo");
        disabled.env("CC", "missing-compiler");
        let disabled = Context::for_command(&disabled, BTreeSet::new()).unwrap();
        assert_eq!(
            inputs(&cap, "linux", target, &disabled).unwrap().identity["enabled"],
            false
        );
    }

    #[test]
    fn dependency_names_and_flags_cannot_be_options() {
        for value in ["--static", "-lgio", "a;touch", "", "CoreWLAN -weak"] {
            assert!(!simple(value, true));
        }
        assert!(simple("gio-2.0", true));
        assert!(flags("-lgio-2.0 -Wl,--as-needed", "libs").is_err());
        assert!(flags("-Xunknown", "cflags").is_err());
        assert!(words("'unterminated").is_err());
    }
    #[test]
    fn archive_rebuild_removes_stale_members_and_tracks_headers() {
        if !["macos", "linux"].contains(&env::consts::OS) {
            return;
        }
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let cap = temp.0.join("renamed-package-1.2.3");
        let ffi = cap.join("src/platform").join(env::consts::OS).join("ffi");
        fs::create_dir_all(ffi.join("sub")).unwrap();
        fs::write(cap.join("Cargo.toml"), "[package]\nname=\"nxrs-wifi\"\n").unwrap();
        for name in ["first", "second"] {
            fs::write(
                ffi.join(format!("{name}.c")),
                format!("int {name}(void) {{return 1;}}\n"),
            )
            .unwrap();
        }
        fs::write(ffi.join("sub/abi.h"), "/* ABI */\n").unwrap();
        let out = temp.0.join("out");
        let rustc = crate::output(Command::new("rustc").arg("-vV")).unwrap();
        let target = rustc
            .lines()
            .find_map(|s| s.strip_prefix("host: "))
            .unwrap();
        let directives = build(&cap, env::consts::OS, target, &out).unwrap();
        assert!(directives.contains(&format!(
            "cargo:rerun-if-changed={}",
            ffi.join("sub/abi.h").display()
        )));
        let archive = out.join("libwifi_native.a");
        assert_eq!(
            crate::output(Command::new("ar").arg("t").arg(&archive))
                .unwrap()
                .lines()
                .filter(|s| s.ends_with(".o"))
                .count(),
            2
        );
        fs::remove_file(ffi.join("second.c")).unwrap();
        build(&cap, env::consts::OS, target, &out).unwrap();
        assert_eq!(
            crate::output(Command::new("ar").arg("t").arg(&archive))
                .unwrap()
                .lines()
                .filter(|s| s.ends_with(".o"))
                .count(),
            1
        );
        let previous = fs::read(&archive).unwrap();
        fs::write(ffi.join("first.c"), "invalid source!\n").unwrap();
        assert!(build(&cap, env::consts::OS, target, &out).is_err());
        assert_eq!(fs::read(archive).unwrap(), previous);
    }
}
