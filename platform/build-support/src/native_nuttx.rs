//! Compile a consuming HAL's private ABI helpers with the configured NuttX flags.
use crate::{hash, native::Context, output, read_json, Result};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};
#[cfg(all(test, unix))]
use std::{env, process::Command};

fn words(value: &Value, key: &str) -> Result<Vec<String>> {
    value[key]
        .as_array()
        .ok_or_else(|| format!("native context requires {key}"))?
        .iter()
        .map(|word| {
            word.as_str()
                .filter(|s| !s.contains('\0'))
                .map(str::to_owned)
                .ok_or_else(|| format!("invalid native {key}"))
        })
        .collect()
}

pub(crate) fn input(path: &Path, directives: &mut Vec<String>) -> Result<()> {
    directives.push(format!("cargo:rerun-if-changed={}", path.display()));
    let resolved = path
        .canonicalize()
        .map_err(|e| format!("native header {}: {e}", path.display()))?;
    if resolved != path {
        directives.push(format!("cargo:rerun-if-changed={}", resolved.display()));
    }
    Ok(())
}

fn headers(path: &Path, seen: &mut BTreeSet<PathBuf>, directives: &mut Vec<String>) -> Result<()> {
    if path.is_dir() {
        let resolved = path.canonicalize().map_err(|e| e.to_string())?;
        if !seen.insert(resolved) {
            return Ok(());
        }
        for entry in fs::read_dir(path).map_err(|e| e.to_string())? {
            headers(&entry.map_err(|e| e.to_string())?.path(), seen, directives)?;
        }
    } else if path.extension().is_some_and(|e| e == "h" || e == "hpp") {
        input(path, directives)?;
    }
    Ok(())
}

// GCC depfiles use Make escaping, not shell quoting. A fixed -MT target
// leaves paths (including spaces, '#' and '$') on the dependency side.
pub(crate) fn dependencies(text: &str) -> Result<Vec<PathBuf>> {
    let text = text
        .strip_prefix("nxrs_native:")
        .ok_or("invalid native dependency file")?;
    let mut chars = text.chars().peekable();
    let mut word = String::new();
    let mut paths = Vec::new();
    while let Some(character) = chars.next() {
        match character {
            '\\' => match chars
                .next()
                .ok_or("unterminated native dependency escape")?
            {
                '\n' => {}
                escaped => word.push(escaped),
            },
            '$' if chars.peek() == Some(&'$') => {
                chars.next();
                word.push('$');
            }
            space if space.is_whitespace() => {
                if !word.is_empty() {
                    paths.push(PathBuf::from(std::mem::take(&mut word)));
                }
            }
            other => word.push(other),
        }
    }
    if !word.is_empty() {
        paths.push(word.into());
    }
    if paths.is_empty() {
        return Err("empty native dependency file".into());
    }
    Ok(paths)
}

pub fn build(
    capability: &Path,
    suffix: &str,
    target: &str,
    out: &Path,
    sources: &[&PathBuf],
    mut directives: Vec<String>,
    environment: &Context,
) -> Result<Vec<String>> {
    directives.push("cargo:rerun-if-env-changed=NXRS_NUTTX_NATIVE_CONFIG".into());
    let context = PathBuf::from(
        environment
            .value("NXRS_NUTTX_NATIVE_CONFIG")?
            .ok_or("NuttX HAL helpers need the configured native context; use cargo nxrs build")?,
    );
    compile(
        &capability.join("src/platform/nuttx/ffi"),
        suffix,
        target,
        out,
        sources,
        &context,
        directives,
        environment,
    )
}

/// Compile explicitly selected fixture sources with the resolved NuttX ABI.
/// This is for target qualification witnesses, not production HAL providers.
pub fn build_sources(
    source_directory: &Path,
    suffix: &str,
    target: &str,
    out: &Path,
    sources: &[&PathBuf],
) -> Result<Vec<String>> {
    let environment = Context::from_build_script()?;
    let context =
        PathBuf::from(environment.value("NXRS_NUTTX_NATIVE_CONFIG")?.ok_or(
            "NuttX fixture helpers need the configured native context; use cargo nxrs build",
        )?);
    compile(
        source_directory,
        suffix,
        target,
        out,
        sources,
        &context,
        vec!["cargo:rerun-if-env-changed=NXRS_NUTTX_NATIVE_CONFIG".into()],
        &environment,
    )
}

struct Configuration {
    data: Value,
    directory: PathBuf,
    include: PathBuf,
    cc: Vec<String>,
    ar: Vec<String>,
    flags: Vec<String>,
}

fn configuration(context: &Path, target: &str) -> Result<Configuration> {
    let data = read_json(context)?;
    if data["schema"] != 1 || data["target"] != target || !target.contains("nuttx") {
        return Err("native context does not match the selected NuttX target".into());
    }
    let directory = PathBuf::from(
        data["directory"]
            .as_str()
            .ok_or("native context directory missing")?,
    );
    let include = PathBuf::from(data["include"].as_str().ok_or("native headers missing")?);
    if !directory.is_absolute()
        || !include.is_absolute()
        || hash(&include.join("nuttx/config.h"))?
            != data["config_sha256"]
                .as_str()
                .ok_or("native config identity missing")?
    {
        return Err("configured NuttX headers changed; rerun the build frontend".into());
    }
    Ok(Configuration {
        cc: words(&data, "cc")?,
        ar: words(&data, "ar")?,
        flags: words(&data, "cflags")?,
        data,
        directory,
        include,
    })
}

pub(crate) fn inputs(target: &str, environment: &Context) -> Result<crate::native::Inputs> {
    let context = PathBuf::from(
        environment
            .value("NXRS_NUTTX_NATIVE_CONFIG")?
            .ok_or("NuttX HAL helpers need the configured native context; use cargo nxrs build")?,
    );
    let configured = configuration(&context, target)?;
    let mut directives = Vec::new();
    headers(&configured.include, &mut BTreeSet::new(), &mut directives)?;
    let mut files: Vec<_> = directives
        .iter()
        .filter_map(|line| line.strip_prefix("cargo:rerun-if-changed="))
        .map(PathBuf::from)
        .collect();
    let mut include_roots = Vec::new();
    crate::native::include_paths(&configured.flags, &configured.directory, &mut include_roots);
    for name in [
        "CPATH",
        "C_INCLUDE_PATH",
        "CPLUS_INCLUDE_PATH",
        "OBJC_INCLUDE_PATH",
    ] {
        if let Some(value) = environment.value(name)? {
            for path in std::env::split_paths(&value) {
                let path = if path.is_absolute() {
                    path
                } else {
                    configured.directory.join(path)
                };
                if path.is_dir() {
                    include_roots.push(path);
                }
            }
        }
    }
    for path in include_roots {
        // The entire configured tree is already enumerated above without
        // temporary Make files. Additional private include roots need a full
        // recursive inventory, including directories for nested shadowing.
        if !path.starts_with(&configured.include) {
            crate::native::tree(&path, &mut files)?;
            files.push(path);
        }
    }
    files.push(context);
    // Inventory content is independent of generated directory timestamps.
    // NuttX Make may create/remove config.h.tmp without changing any header.
    files.sort();
    files.dedup();
    let header_inventory = files.clone();
    let cc = environment.tool_identity(&configured.cc, &configured.directory, &mut files)?;
    let ar = environment.tool_identity(&configured.ar, &configured.directory, &mut files)?;
    let mut variables = environment.environment_inputs(crate::native::COMPILER_ENVIRONMENT)?;
    variables.insert(
        "NXRS_NUTTX_NATIVE_CONFIG".into(),
        environment.value("NXRS_NUTTX_NATIVE_CONFIG")?,
    );
    files.sort();
    files.dedup();
    Ok(crate::native::Inputs {
        files,
        environment: variables,
        identity: json!({"context": configured.data, "headers": header_inventory,
            "compiler": cc, "archiver": ar}),
    })
}

fn compile(
    source_directory: &Path,
    suffix: &str,
    target: &str,
    out: &Path,
    sources: &[&PathBuf],
    context: &Path,
    mut directives: Vec<String>,
    environment: &Context,
) -> Result<Vec<String>> {
    let Configuration {
        directory,
        include,
        cc,
        ar,
        flags,
        ..
    } = configuration(context, target)?;
    directives.push(format!("cargo:rerun-if-changed={}", context.display()));
    // Make creates and removes config.h.tmp even when config.h is unchanged.
    // Watch files, not generated directories. The frontend's context includes
    // the configured header inventory, so additions still invalidate Cargo.
    headers(&include, &mut BTreeSet::new(), &mut directives)?;
    headers(source_directory, &mut BTreeSet::new(), &mut directives)?;
    fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let fresh = crate::TempDir::new(out)?;
    let mut objects = Vec::new();
    for (index, source) in sources.iter().enumerate() {
        let object = fresh.0.join(format!("helper_{index}.o"));
        let dependency_file = fresh.0.join(format!("helper_{index}.d"));
        output(
            environment
                .command(&cc, &directory)?
                .args(&flags)
                .arg("-I")
                .arg(source_directory)
                .arg("-c")
                .arg(source)
                .arg("-o")
                .arg(&object)
                .args(["-MD", "-MT", "nxrs_native", "-MF"])
                .arg(&dependency_file),
        )?;
        for dependency in dependencies(&crate::read(&dependency_file)?)? {
            let path = if dependency.is_absolute() {
                dependency
            } else {
                directory.join(dependency)
            };
            // The architecture's private context headers can live outside include/.
            input(&path, &mut directives)?;
        }
        objects.push(object);
    }
    let name = format!("lib{suffix}_native.a");
    let archive = fresh.0.join(&name);
    output(
        environment
            .command(&ar, &directory)?
            .arg(&archive)
            .args(&objects),
    )?;
    if !archive.is_file() {
        return Err("native archive was not produced".into());
    }
    fs::rename(&archive, out.join(&name)).map_err(|e| e.to_string())?;
    directives.extend([
        format!("cargo:rustc-link-search=native={}", out.display()),
        format!("cargo:rustc-link-lib=static={suffix}_native"),
    ]);
    directives.extend(
        crate::native::COMPILER_ENVIRONMENT
            .iter()
            .map(|name| format!("cargo:rerun-if-env-changed={name}")),
    );
    directives.push("cargo:rerun-if-env-changed=PATH".into());
    directives.sort();
    directives.dedup();
    Ok(directives)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn actual_helpers_use_configured_headers_and_replace_their_own_archive() {
        // Host compiler checks build ownership/invalidations; this is not target ABI qualification.
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let capability = temp.0.join("sensor");
        let source = capability.join("src/platform/nuttx/ffi");
        let include = temp.0.join("headers");
        let out = temp.0.join("out");
        fs::create_dir_all(&source).unwrap();
        fs::create_dir_all(include.join("nuttx")).unwrap();
        let config = include.join("nuttx/config.h");
        fs::write(&config, "#define DEVICE_ABI 17\n").unwrap();
        let helper = source.join("value.c");
        fs::write(
            &helper,
            "#include <nuttx/config.h>\nint device_value(void) { return DEVICE_ABI; }\n",
        )
        .unwrap();
        let context = temp.0.join("native.json");
        let data = json!({"schema":1,"target":"fixture-nuttx","directory":temp.0,"include":include,
            "config_sha256":hash(&config).unwrap(),"cc":["cc"],"ar":["ar","rcs"],"cflags":["-I",include.to_str().unwrap()]});
        crate::write_json(&context, &data).unwrap();
        let mut cargo = Command::new("cargo");
        cargo.env("NXRS_NUTTX_NATIVE_CONFIG", &context);
        let environment = Context::for_command(&cargo, BTreeSet::new()).unwrap();
        let initial = inputs("fixture-nuttx", &environment).unwrap();
        assert!(initial.files.contains(&config));
        assert!(initial.files.contains(&context));
        let temporary_header = include.join("nuttx/config.h.tmp");
        fs::write(&temporary_header, "temporary configuration\n").unwrap();
        assert_eq!(inputs("fixture-nuttx", &environment).unwrap(), initial);
        fs::remove_file(&temporary_header).unwrap();
        let added_header = include.join("nuttx/new.h");
        fs::write(&added_header, "/* newly configured header */\n").unwrap();
        assert_ne!(inputs("fixture-nuttx", &environment).unwrap(), initial);
        fs::remove_file(added_header).unwrap();
        let directives = compile(
            &capability,
            "sensor",
            "fixture-nuttx",
            &out,
            &[&helper],
            &context,
            vec![],
            &environment,
        )
        .unwrap();
        assert!(directives.contains(&format!("cargo:rerun-if-changed={}", config.display())));
        assert!(!directives.contains(&format!("cargo:rerun-if-changed={}", include.display())));
        assert!(!directives.contains(&format!(
            "cargo:rerun-if-changed={}",
            include.join("nuttx").display()
        )));
        let archive = out.join("libsensor_native.a");
        let main = temp.0.join("main.c");
        fs::write(
            &main,
            "int device_value(void); int main(void) { return device_value() == 17 ? 0 : 1; }\n",
        )
        .unwrap();
        let binary = temp.0.join("witness");
        output(
            Command::new("cc")
                .arg(&main)
                .arg(&archive)
                .arg("-o")
                .arg(&binary),
        )
        .unwrap();
        assert!(Command::new(&binary).status().unwrap().success());
        fs::write(&config, "#define DEVICE_ABI 18\n").unwrap();
        assert!(compile(
            &capability,
            "sensor",
            "fixture-nuttx",
            &out,
            &[&helper],
            &context,
            vec![],
            &environment,
        )
        .is_err());
        let mut changed = data;
        changed["config_sha256"] = json!(hash(&config).unwrap());
        crate::write_json(&context, &changed).unwrap();
        compile(
            &capability,
            "sensor",
            "fixture-nuttx",
            &out,
            &[&helper],
            &context,
            vec![],
            &environment,
        )
        .unwrap();
        output(
            Command::new("cc")
                .arg(&main)
                .arg(&archive)
                .arg("-o")
                .arg(&binary),
        )
        .unwrap();
        assert!(!Command::new(&binary).status().unwrap().success());
    }
}
