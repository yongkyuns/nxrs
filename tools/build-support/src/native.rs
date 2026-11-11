//! Optional provider-owned C/Objective-C ABI compilation; invoked on the build host.
use crate::{output, profile::manifest, Result};
use serde_json::Value;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
    sync::atomic::{AtomicUsize, Ordering},
};
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
    if !dir.is_dir() {
        return Ok(());
    }
    for e in fs::read_dir(dir).map_err(|e| e.to_string())? {
        let e = e.map_err(|e| e.to_string())?;
        let p = e.path();
        result.push(p.clone());
        if e.file_type().map_err(|e| e.to_string())?.is_dir() {
            tree(&p, result)?
        }
    }
    Ok(())
}
pub(crate) fn sources<'a>(directory: &Path, paths: &'a [PathBuf]) -> Vec<&'a PathBuf> {
    paths
        .iter()
        .filter(|path| {
            path.parent() == Some(directory)
                && path.is_file()
                && path.extension().is_some_and(|extension| extension == "c" || extension == "m")
        })
        .collect()
}
pub(crate) fn suffix(package: &str) -> Result<String> {
    let suffix = package.strip_prefix("nxrs-").unwrap_or(package).replace('-', "_");
    if !simple(&suffix, false) {
        return Err("unsafe native archive name".into());
    }
    Ok(suffix)
}

pub(crate) fn required_features(manifest: &Value, os: &str) -> Result<Vec<String>> {
    let native = &manifest["package"]["metadata"]["nxrs"]["platform"][os];
    if !native.is_null() && !native.is_object() {
        return Err("native metadata must be a table".into());
    }
    let features = strings(native.get("required-features"), true)?;
    for feature in &features {
        if manifest["features"].get(feature).is_none() {
            return Err(format!("native helper requires undeclared Cargo feature {feature}"));
        }
    }
    Ok(features)
}

pub fn build(capability: &Path, os: &str, target: &str, out: &Path) -> Result<Vec<String>> {
    let source_dir = capability.join("src/platform").join(os).join("ffi");
    let mut watch = source_dir.clone();
    while !watch.is_dir() && watch != capability {
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
    let sources = sources(&source_dir, &paths);
    if sources.is_empty() {
        return Ok(inputs);
    }
    let m = manifest(&capability.join("Cargo.toml"))?;
    let package = m["package"]["name"]
        .as_str()
        .ok_or("missing package name")?;
    // Cargo sources may live in renamed or versioned directories. The archive
    // belongs to the package, independently of its checkout location.
    let suffix = suffix(package)?;
    let native = &m["package"]["metadata"]["nxrs"]["platform"][os];
    let mut enabled = true;
    for feature in required_features(&m, os)? {
        let variable = format!("CARGO_FEATURE_{}", feature.replace('-', "_").to_uppercase());
        inputs.push(format!("cargo:rerun-if-env-changed={variable}"));
        enabled &= env::var_os(variable).is_some();
    }
    if !enabled {
        return Ok(inputs);
    }
    if os == "nuttx" {
        let prepared = crate::prepared_resource::ENVIRONMENT;
        inputs.push(format!("cargo:rerun-if-env-changed={prepared}"));
        if let Some(path) = env::var_os(prepared) {
            let cache = PathBuf::from(path);
            let packages = cache.join(crate::prepared_resource::PACKAGE_LIST);
            inputs.push(format!("cargo:rerun-if-changed={}", packages.display()));
            let selected: std::collections::BTreeSet<PathBuf> =
                serde_json::from_value(crate::read_json(&packages)?)
                    .map_err(|error| format!("invalid prepared native package list: {error}"))?;
            if selected.contains(&capability.canonicalize().map_err(|error| error.to_string())?) {
                inputs.extend(crate::prepared_resource::consume(capability, target, &cache)?);
                return Ok(inputs);
            }
        }
        return crate::native_nuttx::build(capability, &suffix, target, out, &sources, inputs);
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
                    command("PKG_CONFIG", "pkg-config")?
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
    let mut objects = vec![];
    for (i, source) in sources.iter().enumerate() {
        let object = out.join(format!("{suffix}_{i:04}.o"));
        let mut c = if os == "macos" {
            let mut c = Command::new("xcrun");
            c.args(["--sdk", "macosx", "clang", "-target", target]);
            if source.extension().is_some_and(|e| e == "m") {
                c.arg("-fobjc-arc");
            }
            c
        } else {
            let mut c = command("CC", "cc")?;
            c.args(&compile);
            c
        };
        c.args(["-Wall", "-Wextra", "-Werror", "-I"])
            .arg(&source_dir)
            .arg("-c")
            .arg(source)
            .arg("-o")
            .arg(&object);
        output(&mut c)?;
        objects.push(object);
    }
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let fresh = out.join(format!(
        ".native-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir(&fresh).map_err(|e| e.to_string())?;
    let name = format!("lib{suffix}_native.a");
    let archive = fresh.join(&name);
    let result = (|| {
        let mut c = if os == "linux" {
            command("AR", "ar")?
        } else {
            Command::new("ar")
        };
        output(c.arg("crs").arg(&archive).args(&objects))?;
        if !archive.is_file() {
            return Err("native archive command produced no archive".into());
        }
        fs::rename(&archive, out.join(&name)).map_err(|e| e.to_string())
    })();
    let _ = fs::remove_dir_all(&fresh);
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
