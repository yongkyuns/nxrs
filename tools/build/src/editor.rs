//! Configure Zed from the native host profile or a prepared NuttX build.
use nxrs_build::{build, output, read_json, write_json, Result, TempDir};
use serde_json::{json, Value};
use std::{env, fs, path::Path, process::Command};

const DEFAULT_SETTINGS: &str = include_str!("../../../.zed/settings.default.json");

/// Refresh an editor already bound to this output. Other open projects keep
/// their selected context. The new prepared receipts are available either way.
pub fn refresh(root: &Path, out: &Path) -> Result<()> {
    let settings = root.join(".zed/settings.json");
    if !settings.try_exists().map_err(|e| e.to_string())? {
        return Ok(());
    }
    let data = read_json(&settings)?;
    let previous = data
        .pointer("/lsp/rust-analyzer/initialization_options/cargo/extraEnv/NXRS_RESOLVED_PROFILE")
        .and_then(Value::as_str)
        .and_then(|path| Path::new(path).parent());
    let out = out.canonicalize().map_err(|e| e.to_string())?;
    if previous.is_some_and(|path| path == out) {
        apply(
            root,
            &[
                "--build-dir".into(),
                out.to_str().ok_or("invalid editor output path")?.into(),
            ],
            false,
        )?;
    }
    Ok(())
}

pub fn select(root: &Path, args: &[String]) -> Result<()> {
    apply(root, args, true)
}

fn apply(root: &Path, args: &[String], announce: bool) -> Result<()> {
    let directory = root.join(".zed");
    let path = directory.join("settings.json");
    let (cargo, check_on_save) = match args {
        [command] if command == "status" => {
            let settings = if path.try_exists().map_err(|e| e.to_string())? {
                read_json(&path)?
            } else {
                Value::Null
            };
            show_profile(&settings);
            return Ok(());
        }
        [host] if host == "host" => {
            let os = env::consts::OS;
            let profile = build::selection(root, os, None, &[])?;
            profile.validate_target(os)?;
            (
                json!({
                    "extraEnv": {"NXRS_PLATFORM": profile.name(), "CARGO_TARGET_DIR": "target/rust-analyzer"},
                    "buildScripts": {
                        "enable": true,
                        "overrideCommand": ["cargo", "check", "--locked", "--lib", "--message-format=json"]
                    }
                }),
                true,
            )
        }
        [flag, path] if flag == "--build-dir" => {
            let directory = root.join(path);
            let cargo = nuttx_settings(root, &directory).map_err(|error| {
                format!(
                    "{}: {error}. Use a configured Nxrs NuttX build directory",
                    directory.display()
                )
            })?;
            (cargo, false)
        }
        _ => return Err("usage: cargo nxrs editor <host | --build-dir path | status>".into()),
    };
    fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
    let mut settings = if path.try_exists().map_err(|e| e.to_string())? {
        read_json(&path).map_err(|error| {
            format!("{error}. Convert the project settings to JSON before switching")
        })?
    } else {
        serde_json::from_str(DEFAULT_SETTINGS).map_err(|e| e.to_string())?
    };
    let mut options = &mut settings;
    for key in ["lsp", "rust-analyzer", "initialization_options"] {
        options = options
            .as_object_mut()
            .ok_or("invalid Zed language server settings")?
            .entry(key)
            .or_insert_with(|| json!({}));
    }
    let options = options
        .as_object_mut()
        .ok_or("invalid rust-analyzer initialization_options")?;
    options.insert("cargo".into(), cargo);
    options.insert("checkOnSave".into(), json!(check_on_save));
    let check = options.entry("check").or_insert_with(|| json!({}));
    let check = check
        .as_object_mut()
        .ok_or("invalid rust-analyzer check settings")?;
    check.insert("workspace".into(), json!(false));
    check.insert("allTargets".into(), json!(false));
    let temporary = TempDir::new(&directory)?;
    let candidate = temporary.0.join("settings.json");
    write_json(&candidate, &settings)?;
    if fs::read(&path).ok() != fs::read(&candidate).ok() {
        fs::rename(&candidate, &path).map_err(|e| format!("{}: {e}", path.display()))?;
    }
    if announce {
        show_profile(&settings);
    }
    Ok(())
}

fn show_profile(settings: &Value) {
    let cargo = &settings["lsp"]["rust-analyzer"]["initialization_options"]["cargo"];
    match cargo["extraEnv"]["NXRS_PLATFORM"].as_str() {
        Some(name) => {
            let target = cargo["target"].as_str().map(|target| {
                if target.ends_with(".json") {
                    Path::new(target)
                        .file_stem()
                        .and_then(|name| name.to_str())
                        .unwrap_or(target)
                } else {
                    target
                }
            });
            if let Some(target) = target {
                println!("Zed Rust profile: {name} ({target})");
            } else {
                println!("Zed Rust profile: {name}");
            }
        }
        None => println!("No Nxrs editor profile selected"),
    }
}

fn nuttx_settings(root: &Path, directory: &Path) -> Result<Value> {
    let directory = directory.canonicalize().map_err(|e| e.to_string())?;
    let receipt = directory.join("resolved-profile.json");
    let identity = read_json(&receipt)?;
    let target = identity["target"].as_str().ok_or("missing build target")?;
    let profile = nxrs_build_support::resolved::load(&receipt, root, None, target, "nuttx")?;
    let native = directory.join("native-context.json");
    let context = read_json(&native)?;
    if context["target"] != target {
        return Err("native context target differs from the build target".into());
    }
    let include = context["include"]
        .as_str()
        .ok_or("missing NuttX include path")?;
    let compiler = Path::new(context["cc"][0].as_str().ok_or("missing native compiler")?);
    let compiler_directory = compiler
        .parent()
        .ok_or("missing native compiler directory")?;
    let search_path = env::var_os("PATH").unwrap_or_default();
    let search_path = env::join_paths(
        std::iter::once(compiler_directory.to_path_buf()).chain(env::split_paths(&search_path)),
    )
    .map_err(|e| e.to_string())?;
    let std = read_json(&directory.join("std-source.json"))?;
    let rustc = std["rustc"].as_str().ok_or("missing NuttX Rust compiler")?;
    let sysroot = Path::new(rustc)
        .parent()
        .and_then(Path::parent)
        .ok_or("missing Rust sysroot")?;
    if !Path::new(rustc).is_file()
        || !compiler.is_file()
        || !Path::new(include).join("nuttx/config.h").is_file()
    {
        return Err("NuttX compiler or configured headers are missing".into());
    }
    let custom = directory.join(format!("{target}.json"));
    let target = if custom.is_file() {
        custom.to_str().ok_or("non-UTF-8 Rust target path")?
    } else {
        target
    };
    let selection = read_json(&directory.join("provider-selection.json"))?;
    let resources = selection["native-resources"]
        .as_array()
        .ok_or("missing native resource preparation; rerun cargo nxrs build")?;
    let prepared = directory.join("native-resources");
    if !resources.is_empty() && !prepared.is_dir() {
        return Err("prepared native resources are missing; rerun cargo nxrs build".into());
    }
    let manifest = Path::new(
        selection["app_manifest"]
            .as_str()
            .ok_or("missing build manifest")?,
    );
    if !manifest.starts_with(root) {
        return Err("the selected application's manifest is outside the Nxrs workspace".into());
    }
    let package = selection["app_package"]
        .as_str()
        .ok_or("missing build package")?;
    let editor_path = directory.join(".editor-cargo.json");
    let editor = if editor_path.try_exists().map_err(|e| e.to_string())? {
        read_json(&editor_path)?
    } else {
        Value::Null
    };
    let features: Vec<String> = serde_json::from_value(
        editor
            .get("features")
            .unwrap_or(&selection["model_features"])
            .clone(),
    )
    .map_err(|_| "missing build feature selection")?;
    let no_defaults = editor["no_default_features"].as_bool().unwrap_or(false);
    let sdk_cargo = sysroot.join("bin/cargo");
    let cargo = editor["cargo"]
        .as_str()
        .or_else(|| sdk_cargo.to_str())
        .ok_or("missing Cargo tool")?;
    let workspace = output(
        Command::new(cargo)
            .current_dir(root)
            .args([
                "locate-project",
                "--workspace",
                "--message-format",
                "plain",
                "--manifest-path",
            ])
            .arg(manifest),
    )?;
    if Path::new(workspace.trim()).canonicalize().ok() != Some(root.join("Cargo.toml")) {
        return Err("the selected application is not a member of the Nxrs workspace".into());
    }
    let mut command = vec![
        cargo,
        "check",
        "--locked",
        "-p",
        package,
        "--target",
        target,
        "-Zbuild-std=std,panic_abort",
        "--message-format=json",
    ];
    for feature in &features {
        command.extend(["--features", feature]);
    }
    if no_defaults {
        command.push("--no-default-features");
    }
    // Cargo metadata loads the whole workspace. Scope bare features to the
    // selected package, just as the check command's -p does.
    let metadata_features: Vec<_> = features
        .iter()
        .map(|feature| {
            if feature.contains('/') {
                feature.clone()
            } else {
                format!("{package}/{feature}")
            }
        })
        .collect();
    let mut extra_env = json!({
        "NXRS_PLATFORM": profile.name(),
        "NXRS_RESOLVED_PROFILE": receipt,
        "CARGO_TARGET_DIR": "target/rust-analyzer",
        "PATH": search_path.to_str().ok_or("non-UTF-8 tool search path")?,
        "RUSTC": rustc,
        "RUSTC_BOOTSTRAP": "1",
        "NUTTX_STD_SYSROOT": sysroot,
        "NXRS_NUTTX_INCLUDE": include,
        "NXRS_NUTTX_NATIVE_CONFIG": native,
    });
    if !resources.is_empty() {
        extra_env[nxrs_build_support::prepared_resource::ENVIRONMENT] = json!(prepared);
    }
    if let Some(path) = selection["configuration"].as_str() {
        extra_env[nxrs_build_support::preparation::ENVIRONMENT] = json!(path);
    }
    for name in ["NXRS_PICO_SDK", "NXRS_TLS_ARCHIVE"] {
        if let Some(path) = editor["sdk"].get(name) {
            extra_env[name] = path.clone();
        }
    }
    Ok(json!({
        "target": target,
        "sysroot": sysroot,
        "features": metadata_features,
        "noDefaultFeatures": no_defaults,
        "extraEnv": extra_env,
        "buildScripts": {"enable": true, "overrideCommand": command}
    }))
}
