//! NuttX build sequencing. Upstream Make/Kconfig and compilers remain external tools.
use crate::{
    boot,
    build::{self, App, Options},
    deployment, find_program, hash, output,
    profile::Profile,
    read, run, sdk, source, write_json, Result,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};
fn string<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| format!("missing nonempty {key}"))
}
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
fn line(key: &str, value: &str) -> String {
    if value == "n" {
        format!("# CONFIG_{key} is not set")
    } else {
        format!("CONFIG_{key}={value}")
    }
}
pub fn configure(path: &Path, values: &BTreeMap<String, String>) -> Result<()> {
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
        let key = current
            .strip_prefix("CONFIG_")
            .and_then(|s| s.split_once('='))
            .map(|(k, _)| k)
            .or_else(|| {
                current
                    .strip_prefix("# CONFIG_")
                    .and_then(|s| s.strip_suffix(" is not set"))
            });
        if !key.is_some_and(|k| values.contains_key(k)) {
            result.push_str(current);
            result.push('\n');
        }
    }
    for (key, value) in values {
        result.push_str(&line(key, value));
        result.push('\n');
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
fn standard() -> BTreeMap<String, String> {
    let mut v = BTreeMap::new();
    for key in [
        "EXAMPLES_NXRS_STD_APP",
        "FS_LARGEFILE",
        "DEV_URANDOM",
        "SCHED_WAITPID",
        "SCHED_HAVE_PARENT",
        "SCHED_CHILD_STATUS",
        "NSH_DISABLEBG",
        "NSH_ARGCAT",
        "TLS_GLOBAL_KEYS",
    ] {
        v.insert(key.into(), "y".into());
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
pub fn stage_helpers(sources: &[PathBuf], app: &Path) -> Result<(Vec<PathBuf>, Vec<String>)> {
    let mut unique = vec![];
    let mut objects = vec![];
    for source in sources {
        let source = source.canonicalize().map_err(|e| e.to_string())?;
        if source.extension().is_none_or(|s| s != "c") {
            return Err("target helper must be an existing .c source".into());
        }
        if unique.contains(&source) {
            continue;
        }
        let name = format!("target_helper_{}.c", unique.len());
        fs::copy(&source, app.join(&name)).map_err(|e| e.to_string())?;
        for entry in fs::read_dir(source.parent().unwrap()).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            let header = entry.path();
            if header.extension().is_some_and(|s| s == "h") {
                let to = app.join(entry.file_name());
                if to.exists()
                    && fs::read(&to).map_err(|e| e.to_string())?
                        != fs::read(&header).map_err(|e| e.to_string())?
                {
                    return Err(format!(
                        "Conflicting target helper header: {}",
                        header.display()
                    ));
                }
                fs::copy(header, to).map_err(|e| e.to_string())?;
            }
        }
        unique.push(source);
        objects.push(name);
    }
    Ok((unique, objects))
}
struct Tools {
    sdk: &'static str,
    cargo: PathBuf,
    rustc: PathBuf,
    prefix: String,
    esp: bool,
    path: std::ffi::OsString,
}
fn bin_directories(root: &Path, depth: usize, tool: &str, paths: &mut Vec<PathBuf>) -> Result<()> {
    if !root.is_dir() {
        return Ok(());
    }
    if root.file_name().is_some_and(|name| name == "bin") {
        if root.join(tool).is_file() {
            paths.push(root.to_path_buf());
        }
        return Ok(());
    }
    if depth == 0 {
        return Ok(());
    }
    let mut directories = Vec::new();
    for entry in fs::read_dir(root).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        if entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            directories.push(entry.path());
        }
    }
    directories.sort();
    for directory in directories {
        bin_directories(&directory, depth - 1, tool, paths)?;
    }
    Ok(())
}
impl Tools {
    fn select(root: &Path, target: &str, profile: &Profile) -> Result<Self> {
        let esp = target == "xtensa-esp32s3-nuttx";
        if !esp && target != "thumbv8m.main-nuttx-eabi" {
            return Err("unsupported NuttX std target".into());
        }
        let qemu = match profile.data.get("requires-qemu") {
            None => true,
            Some(v) => v.as_bool().ok_or("requires-qemu must be boolean")?,
        };
        let native = env::consts::OS == "linux" && env::consts::ARCH == "x86_64";
        let explicit_esp = esp && env::var("NXRS_ESP32S3_NATIVE_TOOLS").as_deref() == Ok("1");
        let explicit_arm = env::consts::OS == "macos"
            && !esp
            && !qemu
            && env::var("NXRS_NUTTX_NATIVE_TOOLS").as_deref() == Ok("1");
        if !(native || explicit_esp || explicit_arm) {
            return Err("NuttX requires Linux x86_64 or an explicit supported native-board tool environment".into());
        }
        let sdk = if esp {
            "esp-1.90.0.0"
        } else {
            "nightly-2025-09-15"
        };
        let bundle = env::var_os("NXRS_QEMU_TOOLS_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("target/qemu-tools"));
        let mut paths = Vec::new();
        if esp {
            bin_directories(&bundle.join("gcc"), 6, "xtensa-esp-elf-gcc", &mut paths)?;
            bin_directories(&bundle.join("qemu"), 6, "qemu-system-xtensa", &mut paths)?;
        }
        paths.extend(env::split_paths(&env::var_os("PATH").unwrap_or_default()));
        let path = env::join_paths(paths).map_err(|e| e.to_string())?;
        let cargo = PathBuf::from(
            output(Command::new("rustup").args([
                "which",
                "--toolchain",
                if esp { "1.90.0" } else { sdk },
                "cargo",
            ]))?
            .trim(),
        );
        let rustc = if esp {
            env::var_os("RUSTC")
                .map(PathBuf::from)
                .unwrap_or_else(|| bundle.join("rust-installed/bin/rustc"))
        } else {
            PathBuf::from(
                output(Command::new("rustup").args(["which", "--toolchain", sdk, "rustc"]))?.trim(),
            )
        };
        if !rustc.is_file() {
            return Err(
                "missing pinned source Rust compiler; set RUSTC or prepare the tool bundle".into(),
            );
        }
        let prefix = string(&profile.data, "crossdev")?.to_owned();
        for tool in ["git", "make", "rustup"] {
            find_program(tool)?;
        }
        for suffix in ["gcc", "ld", "objcopy", "readelf", "nm", "size"] {
            crate::find_program_on_path(&format!("{prefix}{suffix}"), &path)?;
        }
        if esp {
            crate::find_program_on_path("esptool.py", &path)?;
            if qemu {
                crate::find_program_on_path("qemu-system-xtensa", &path)?;
            }
        }
        Ok(Self {
            sdk,
            cargo,
            rustc,
            prefix,
            esp,
            path,
        })
    }
    fn command(&self, name: &str) -> Command {
        let mut c = Command::new(name);
        c.env("PATH", &self.path)
            .env("LC_ALL", "C")
            .env("RUSTC_BOOTSTRAP", "1")
            .env(
                "RUSTUP_TOOLCHAIN",
                if self.esp { "1.90.0" } else { self.sdk },
            );
        c
    }
    fn program(&self, name: &str) -> Result<PathBuf> {
        crate::find_program_on_path(name, &self.path)
    }
}
pub fn build(root: &Path, app: &App, p: &Profile, out: &Path, options: &Options) -> Result<()> {
    options.validate_for_backend("nuttx")?;
    let deployment = deployment::validate(root, &app.manifest, p, "nuttx")?;
    let target = string(&p.data, "target")?;
    let board = string(&p.data, "board")?;
    let machine = string(&p.data, "machine")?;
    let image = string(&p.data, "image")?;
    let settings = p.data.get("build").unwrap_or(&Value::Null);
    if !settings.is_null()
        && !settings.as_object().is_some_and(|o| {
            o.keys().all(|s| {
                [
                    "abi-profile",
                    "size-optimized",
                    "trace-only-backtrace",
                    "panic-immediate-abort",
                ]
                .contains(&s.as_str())
            })
        })
    {
        return Err("invalid platform build settings".into());
    }
    let flag = |key: &str, default| match settings.get(key) {
        None => Ok(default),
        Some(v) => v
            .as_bool()
            .ok_or_else(|| format!("build.{key} must be boolean")),
    };
    let size = flag("size-optimized", options.size)?;
    let trace = flag("trace-only-backtrace", options.trace)?;
    let immediate = flag("panic-immediate-abort", options.immediate)?;
    if immediate && !trace {
        return Err("immediate abort requires trace-only backtrace".into());
    }
    let abi = match settings.get("abi-profile") {
        Some(value) => value.as_str().ok_or("build.abi-profile must be a string")?,
        None => options.abi.as_deref().unwrap_or("active"),
    };
    if !["active", "minimal"].contains(&abi) {
        return Err("invalid ABI profile".into());
    }
    let signed = p.data.get("mcuboot").is_some();
    let signing = if signed {
        Some(boot::Signing::environment()?)
    } else {
        None
    };
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
    for key in &options.enable {
        values.insert(key.into(), "y".into());
        requirements.push(line(key, "y"));
    }
    for key in &options.disable {
        values.insert(key.into(), "n".into());
        requirements.push(line(key, "n"));
    }
    let standard = standard();
    requirements.extend(standard.iter().map(|(k, v)| line(k, v)));
    requirements.push("CONFIG_BUILD_FLAT=y".into());
    values.extend(standard);
    let forbid = match k.get("forbid-regex") {
        None => "",
        Some(v) => v.as_str().ok_or("forbid-regex must be a string")?,
    };
    if !forbid.is_empty() {
        regex::Regex::new(forbid).map_err(|e| e.to_string())?;
    }
    let jobs = env::var("NXRS_NUTTX_BUILD_JOBS")
        .unwrap_or("4".into())
        .parse::<usize>()
        .ok()
        .filter(|n| *n > 0 && *n <= 128)
        .ok_or("build jobs must be between 1 and 128")?;
    let tools = Tools::select(root, target, p)?;
    let mut sources = p.native_helpers(root)?;
    sources.extend(options.helpers.iter().map(|path| {
        if path.is_absolute() {
            path.clone()
        } else {
            root.join(path)
        }
    }));
    for path in &sources {
        if !path.is_file() {
            return Err(format!("missing native helper: {}", path.display()));
        }
    }
    let model_features = p.model_features(root, &app.manifest)?;
    let out = build::prepare_output(root, out)?;
    for input in std::iter::once(&app.manifest)
        .chain(sources.iter())
        .chain(signing.iter().flat_map(|s| [&s.key, &s.root]))
    {
        if input
            .canonicalize()
            .map_err(|e| e.to_string())?
            .starts_with(&out)
        {
            return Err(
                "build inputs and signing keys must be outside the owned output directory".into(),
            );
        }
    }
    if env::var_os("NXRS_SHARED_NUTTX_STD_SYSROOT").is_some_and(|p| {
        PathBuf::from(p)
            .canonicalize()
            .is_ok_and(|p| p.starts_with(&out))
    }) {
        return Err("shared SDK must be outside the owned output directory".into());
    }
    reset_output(&out)?;
    let nuttx = out.join("nuttx");
    let apps = out.join("apps");
    for (component, dest) in [("nuttx", &nuttx), ("nuttx-apps", &apps)] {
        let rev = source::revision(root, component)?;
        source::archive_revision(&root.join("external").join(component), &rev, dest)?;
        source::apply_series(
            root,
            dest,
            component,
            &rev,
            &out.join(format!("{component}-patches.json")),
            false,
        )?;
    }
    source::stamp(root, &nuttx, &source::revision(root, "nuttx")?)?;
    let entry = apps.join("examples/nxrs_std_app");
    fs::create_dir_all(&entry).map_err(|e| e.to_string())?;
    for file in fs::read_dir(root.join("platform/nuttx/std-app")).map_err(|e| e.to_string())? {
        let file = file.map_err(|e| e.to_string())?;
        fs::copy(file.path(), entry.join(file.file_name())).map_err(|e| e.to_string())?;
    }
    let (sources, objects) = stage_helpers(&sources, &entry)?;
    let radio = match p.data.get("radio-firmware") {
        None => "",
        Some(value) => value.as_str().ok_or("radio-firmware must be a string")?,
    };
    let mut radio_inputs = Vec::new();
    if !radio.is_empty() {
        let (wifi, length, bt) = crate::radio::prepare(root, &out, radio)?;
        radio_inputs.extend([out.join("radio-firmware.json"), wifi.clone()]);
        values.insert(
            "CYW43439_FIRMWARE_BIN_PATH".into(),
            format!("\"{}\"", wifi.display()),
        );
        values.insert("CYW43439_FIRMWARE_LEN".into(), length.to_string());
        if let Some(bt) = bt {
            radio_inputs.push(bt.clone());
            values.insert(
                "CYW43439_BLUETOOTH_FIRMWARE_BIN_PATH".into(),
                format!("\"{}\"", bt.display()),
            );
        }
    }
    run(tools
        .command("bash")
        .current_dir(&nuttx)
        .arg("tools/configure.sh")
        .args(["-l", board]))?;
    configure(&nuttx.join(".config"), &values)?;
    run(tools
        .command("make")
        .current_dir(&nuttx)
        .arg("olddefconfig"))?;
    required(&read(&nuttx.join(".config"))?, &requirements, forbid)?;
    if read(&nuttx.join(".config"))?
        .lines()
        .any(|line| line == "CONFIG_ETC_ROMFS=y")
    {
        tools.program("genromfs")?;
    }
    if read(&nuttx.join(".config"))?
        .lines()
        .any(|s| s == "CONFIG_SIG_DEFAULT=y")
    {
        return Err("unqualified SIG_DEFAULT configuration".into());
    }
    run(tools.command("make").current_dir(&nuttx).args([
        "include/nuttx/config.h",
        &format!("CROSSDEV={}", tools.prefix),
    ]))?;
    fs::copy(nuttx.join(".config"), out.join("resolved.config")).map_err(|e| e.to_string())?;
    write_json(&out.join("provider-selection.json"), &deployment)?;
    let sysroot = if let Some(shared) = env::var_os("NXRS_SHARED_NUTTX_STD_SYSROOT") {
        let shared = PathBuf::from(shared)
            .canonicalize()
            .map_err(|e| e.to_string())?;
        if !shared.join("bin/rustc").is_file() {
            return Err("missing shared NuttX std sysroot".into());
        }
        for file in
            fs::read_dir(shared.parent().ok_or("sysroot parent")?).map_err(|e| e.to_string())?
        {
            let file = file.map_err(|e| e.to_string())?;
            let name = file.file_name();
            let name = name.to_string_lossy();
            if name == "std-patch.json"
                || ((name.starts_with("std-") || name.starts_with("libc-"))
                    && name.ends_with(".patch"))
            {
                fs::copy(file.path(), out.join(name.as_ref())).map_err(|e| e.to_string())?;
            }
        }
        shared
    } else {
        let source = PathBuf::from(
            output(
                tools
                    .command(tools.rustc.to_str().ok_or("compiler path UTF-8")?)
                    .args(["--print", "sysroot"]),
            )?
            .trim(),
        );
        sdk::prepare(root, &source, &out, tools.sdk)?
    };
    let rustc = sysroot.join("bin/rustc");
    let mut target_arg = PathBuf::from(target);
    if tools.esp {
        let bare = output(
            tools
                .command(rustc.to_str().ok_or("compiler path UTF-8")?)
                .args([
                    "-Z",
                    "unstable-options",
                    "--print",
                    "target-spec-json",
                    "--target",
                    "xtensa-esp32s3-none-elf",
                ]),
        )?;
        fs::write(out.join("xtensa-bare-target.json"), &bare).map_err(|e| e.to_string())?;
        let mut spec: Value = serde_json::from_str(&bare).map_err(|e| e.to_string())?;
        if spec["arch"] != "xtensa"
            || spec["cpu"] != "esp32s3"
            || !(spec["target-pointer-width"] == "32" || spec["target-pointer-width"] == 32)
            || spec["max-atomic-width"] != 32
        {
            return Err("unqualified Xtensa target specification".into());
        }
        for (key, value) in [
            ("os", json!("nuttx")),
            ("target-family", json!(["unix"])),
            ("executables", json!(true)),
            ("has-thread-local", json!(false)),
            ("linker-flavor", json!("ld")),
            ("linker", json!("xtensa-esp32s3-elf-ld")),
            ("relocation-model", json!("static")),
            (
                "metadata",
                json!({"description":format!("Nxrs deployment {}-{}",app.bin,p.name()),"std":true}),
            ),
        ] {
            spec[key] = value;
        }
        for key in ["pre-link-args", "late-link-args", "post-link-args"] {
            spec.as_object_mut().unwrap().remove(key);
        }
        target_arg = out.join(format!("{target}.json"));
        write_json(&target_arg, &spec)?;
        let downloads = env::var_os("NXRS_QEMU_TOOLS_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("target/qemu-tools"))
            .join("downloads.sha256");
        fs::copy(downloads, out.join("downloads.sha256")).map_err(|e| e.to_string())?;
    }
    let target_dir = env::var_os("NXRS_SHARED_CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| out.join("cargo"));
    let linker = out.join("nxrs-nuttx-link");
    fs::copy(env::current_exe().map_err(|e| e.to_string())?, &linker).map_err(|e| e.to_string())?;
    // Encoded flags preserve paths containing spaces without a shell/linker shim.
    let mut flags = vec![
        "-C".to_owned(),
        "panic=abort".into(),
        "-C".into(),
        format!("linker={}", linker.display()),
    ];
    if env::var("NXRS_NUTTX_DIAGNOSTIC_UNWIND").as_deref() == Ok("1") {
        flags.extend(["-C".into(), "force-unwind-tables=yes".into()]);
    }
    let mut cargo = tools.command(tools.cargo.to_str().ok_or("cargo path UTF-8")?);
    cargo
        .current_dir(root)
        .args(["build", "--locked", "--release", "--manifest-path"])
        .arg(&app.manifest)
        .args(["-p", &app.package, "--bin", &app.bin, "--target"])
        .arg(&target_arg)
        .args([
            "-Zbuild-std=std,panic_abort",
            "--message-format=json-render-diagnostics",
        ])
        .env("NXRS_PLATFORM", p.name())
        .env("NUTTX_STD_SYSROOT", &sysroot)
        .env("RUSTC", &rustc)
        .env("CARGO_BUILD_RUSTC", &rustc)
        .env("CARGO_TARGET_DIR", &target_dir)
        .env("NXRS_NUTTX_INCLUDE", nuttx.join("include"))
        .env("NXRS_APP_COMMAND", &app.command)
        .env("NXRS_APP_PRIORITY", app.priority.to_string())
        .env("NXRS_APP_STACKSIZE", app.stack.to_string())
        .env("NUTTX_STD_LINK_LOG", out.join("rust-link.json"))
        .env(
            "CARGO_PROFILE_RELEASE_LTO",
            if size { "fat" } else { "false" },
        )
        .env("CARGO_PROFILE_RELEASE_DEBUG", if size { "0" } else { "1" })
        .env("CARGO_PROFILE_RELEASE_STRIP", "none")
        .env_remove("RUSTFLAGS")
        .env("CARGO_ENCODED_RUSTFLAGS", flags.join("\x1f"));
    options.configure_cargo(&mut cargo, &model_features);
    if tools.esp {
        cargo.env(
            "NUTTX_STD_GNU_LINKER",
            tools.program("xtensa-esp32s3-elf-ld")?,
        );
    } else {
        cargo.env_remove("NUTTX_STD_GNU_LINKER");
    }
    if trace {
        cargo.arg(format!(
            "-Zbuild-std-features=backtrace-trace-only,optimize_for_size{}",
            if immediate {
                ",panic_immediate_abort"
            } else {
                ""
            }
        ));
    }
    if let Some(config) = env::var_os("NXRS_CARGO_CONFIG") {
        cargo.arg("--config").arg(config);
    }
    build::cargo_messages(&mut cargo, &out.join("cargo-messages.jsonl"))?;
    build::std_source(&out.join("cargo-messages.jsonl"), &sysroot, &rustc, &out)?;
    let elf = target_dir.join(target).join("release").join(&app.bin);
    let header = output(
        tools
            .command(&format!("{}readelf", tools.prefix))
            .args(["-h"])
            .arg(&elf),
    )?;
    if !header.contains("REL (Relocatable file)")
        || !header.lines().any(|s| {
            s.trim()
                .strip_prefix("Machine:")
                .is_some_and(|s| s.trim() == machine)
        })
    {
        return Err("Rust input ELF machine/type mismatch".into());
    }
    fs::write(out.join("rust-elf-header.txt"), header).map_err(|e| e.to_string())?;
    let symbols = output(tools.command(&format!("{}nm", tools.prefix)).arg(&elf))?;
    require_symbols(&symbols, &[("main", "T")])?;
    if abi == "active" {
        require_symbols(
            &symbols,
            &[
                ("pthread_create", "U"),
                ("pthread_join", "U"),
                ("clock_gettime", "U"),
            ],
        )?;
    }
    fs::write(out.join("rust-symbols.txt"), symbols).map_err(|e| e.to_string())?;
    let mut make_args = vec![
        format!("CROSSDEV={}", tools.prefix),
        format!("NXRS_STD_ELF={}", elf.display()),
        format!("NXRS_APP_COMMAND={}", app.command),
        format!("NXRS_APP_PRIORITY={}", app.priority),
        format!("NXRS_APP_STACKSIZE={}", app.stack),
    ];
    if !objects.is_empty() {
        make_args.push(format!("NXRS_TARGET_C_SOURCE={}", objects.join(" ")));
    }
    if tools.esp {
        make_args.push("ESPTOOL_BINDIR=.".into());
        if let Some(cache) = env::var_os("NXRS_ESP_HAL_3RDPARTY_CACHE") {
            let cache = PathBuf::from(cache);
            let revision = output(source::git(&cache).args(["rev-parse", "HEAD"]))?;
            if env::var("NXRS_ESP_HAL_3RDPARTY_REVISION").is_ok_and(|s| s != revision.trim()) {
                return Err("unexpected Espressif HAL cache revision".into());
            }
            copy_hal_cache(
                &cache,
                &nuttx.join("arch/xtensa/src/chip/esp-hal-3rdparty"),
                revision.trim(),
            )?;
        }
    }
    run(tools
        .command("make")
        .arg("-C")
        .arg(&nuttx)
        .arg("context")
        .args(&make_args))?;
    if signed
        && read(&nuttx.join(".config"))?
            .lines()
            .any(|s| s == "CONFIG_BOOT_MCUBOOT=y")
    {
        source::apply_series(
            root,
            &apps.join("boot/mcuboot/mcuboot"),
            "mcuboot",
            boot::MCUBOOT_REVISION,
            &out.join("mcuboot-patches.json"),
            false,
        )?;
    }
    if tools.esp {
        let hal = isolated_hal(&nuttx, &nuttx.join("arch/xtensa/src/chip/esp-hal-3rdparty"))?;
        let rev = output(source::git(&hal).args(["rev-parse", "HEAD"]))?;
        source::apply_series(
            root,
            &hal,
            "esp-hal-3rdparty",
            rev.trim(),
            &out.join("esp-hal-patches.json"),
            true,
        )?;
    }
    // Qualification is an independent test program, not build configuration policy.
    run(tools
        .command("python3")
        .current_dir(root)
        .arg("tests/nuttx-std/check-abi.py")
        .arg("--self-test"))?;
    let mut check = tools.command("python3");
    check
        .current_dir(root)
        .arg("tests/nuttx-std/check-abi.py")
        .arg("--out")
        .arg(&out)
        .args(["--target", target])
        .env("RUSTC", &rustc)
        .env("NUTTX_STD_SYSROOT", &sysroot)
        .env("CARGO_TARGET_DIR", &target_dir);
    if tools.esp {
        check.arg("--target-spec").arg(&target_arg);
    }
    run(&mut check)?;
    run(tools
        .command("make")
        .arg("-C")
        .arg(&nuttx)
        .arg(format!("-j{jobs}"))
        .args(&make_args))?;
    fs::copy(nuttx.join(".config"), out.join("resolved.config")).map_err(|e| e.to_string())?;
    if let Some(signing) = signing {
        signing.sign(
            p,
            &nuttx.join("nuttx.hex"),
            &nuttx.join("nuttx.signed.bin"),
            false,
        )?;
    }
    let symbols = output(
        tools
            .command(&format!("{}nm", tools.prefix))
            .arg(nuttx.join("nuttx")),
    )?;
    let entry_symbol = format!("{}_main", app.command);
    let mut needed = vec![(entry_symbol.as_str(), "TtWw"), ("nx_start", "TtWw")];
    if abi == "active" {
        needed.extend([
            ("pthread_create", "TtWw"),
            ("pthread_join", "TtWw"),
            ("clock_gettime", "TtWw"),
        ]);
    }
    require_symbols(&symbols, &needed)?;
    fs::write(out.join("symbols.txt"), symbols).map_err(|e| e.to_string())?;
    for (tool, args, file) in [
        ("size", vec!["-A"], "image-size.txt"),
        ("readelf", vec!["-h", "-l", "-A"], "image-layout.txt"),
    ] {
        fs::write(
            out.join(file),
            output(
                tools
                    .command(&format!("{}{tool}", tools.prefix))
                    .args(args)
                    .arg(nuttx.join("nuttx")),
            )?,
        )
        .map_err(|e| e.to_string())?;
    }
    let image = nuttx.join(if signed { "nuttx.signed.bin" } else { image });
    if image.metadata().map_err(|e| e.to_string())?.len() == 0 {
        return Err("empty firmware image".into());
    }
    let mut provenance = vec![
        format!("deployment={}-{}", app.bin, p.name()),
        format!(
            "nxrs={}",
            output(source::git(root).args(["rev-parse", "HEAD"]))?.trim()
        ),
        format!(
            "nxrs_dirty={}",
            if output(source::git(root).args(["status", "--porcelain", "--untracked-files=no"]))?
                .is_empty()
            {
                "no"
            } else {
                "yes"
            }
        ),
        format!("nuttx={}", source::revision(root, "nuttx")?),
        format!("apps={}", source::revision(root, "nuttx-apps")?),
        format!("app_package={}", app.package),
        format!("app_bin={}", app.bin),
        format!("platform={}", p.name()),
        format!(
            "platform_profile={}",
            p.sources
                .last()
                .ok_or("profile source missing")?
                .strip_prefix(root)
                .map_err(|e| e.to_string())?
                .display()
        ),
        "provider_selection=provider-selection.json".into(),
        format!("app_command={}", app.command),
        format!("abi_profile={abi}"),
        format!("size_optimized={}", size as u8),
        format!("trace_only_backtrace={}", trace as u8),
        format!("panic_immediate_abort={}", immediate as u8),
        format!("target={target}"),
        format!("board={board}"),
    ];
    provenance.extend(options.enable.iter().map(|s| format!("kconfig_enable={s}")));
    provenance.extend(
        options
            .disable
            .iter()
            .map(|s| format!("kconfig_disable={s}")),
    );
    for source in &sources {
        provenance.push(format!(
            "target_c_source={}\n{}  {}",
            source.display(),
            hash(source)?,
            source.display()
        ));
    }
    provenance.push(
        output(
            tools
                .command(rustc.to_str().unwrap())
                .args(["--version", "--verbose"]),
        )?
        .trim()
        .into(),
    );
    provenance.push(
        output(
            tools
                .command(&format!("{}gcc", tools.prefix))
                .arg("--version"),
        )?
        .lines()
        .next()
        .ok_or("missing C compiler version")?
        .into(),
    );
    if tools.esp
        && p.data
            .get("requires-qemu")
            .and_then(Value::as_bool)
            .unwrap_or(true)
    {
        provenance.push(
            output(tools.command("qemu-system-xtensa").arg("--version"))?
                .lines()
                .next()
                .ok_or("missing QEMU version")?
                .into(),
        );
    }
    fs::write(out.join("provenance.txt"), provenance.join("\n") + "\n")
        .map_err(|e| e.to_string())?;
    let mut inputs = vec![
        nuttx.join("nuttx"),
        image.clone(),
        elf,
        app.manifest.clone(),
        out.join("resolved.config"),
        out.join("provider-selection.json"),
    ];
    inputs.extend(p.sources.clone());
    inputs.extend(radio_inputs);
    inputs.extend(sources);
    for file in fs::read_dir(&entry).map_err(|e| e.to_string())? {
        let path = file.map_err(|e| e.to_string())?.path();
        if path.extension().is_some_and(|s| s == "h") {
            inputs.push(path);
        }
    }
    if tools.esp {
        inputs.push(target_arg);
    }
    let mut hashes = String::new();
    for path in inputs {
        hashes.push_str(&format!("{}  {}\n", hash(&path)?, path.display()));
    }
    fs::write(out.join("binaries.sha256"), hashes).map_err(|e| e.to_string())?;
    println!("NuttX image: {}", image.display());
    Ok(())
}
pub fn isolated_hal(nuttx: &Path, hal: &Path) -> Result<PathBuf> {
    let root = nuttx.canonicalize().map_err(|e| e.to_string())?;
    let hal = hal.canonicalize().map_err(|e| e.to_string())?;
    if hal == root || !hal.starts_with(root) {
        return Err("Espressif source escapes the isolated NuttX build".into());
    }
    Ok(hal)
}
fn reset_output(out: &Path) -> Result<()> {
    for name in ["nuttx", "apps", "cargo", "toolchain"] {
        crate::remove_dir(&out.join(name))?;
    }
    for entry in fs::read_dir(out).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let path = entry.path();
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        if (kind.is_file() || kind.is_symlink())
            && entry.file_name() != ".nxrs-build.json"
            && path.extension().is_some_and(|e| {
                ["json", "jsonl", "txt", "patch", "sha256", "config"]
                    .iter()
                    .any(|s| e == *s)
            })
        {
            crate::remove_file(&path)?;
        }
    }
    Ok(())
}
fn require_symbols(symbols: &str, required: &[(&str, &str)]) -> Result<()> {
    for (name, kinds) in required {
        if !symbols.lines().any(|line| {
            let words: Vec<_> = line.split_whitespace().collect();
            words.len() >= 2
                && words[words.len() - 1] == *name
                && words[words.len() - 2].len() == 1
                && kinds.contains(words[words.len() - 2])
        }) {
            return Err(format!("ELF missing required symbol {name}"));
        }
    }
    Ok(())
}
pub fn copy_hal_cache(cache: &Path, destination: &Path, revision: &str) -> Result<()> {
    let cache = cache.canonicalize().map_err(|e| e.to_string())?;
    if !cache.join(".git").is_dir()
        || output(source::git(&cache).args(["rev-parse", "--show-toplevel"]))?.trim()
            != cache.to_str().ok_or("cache UTF-8")?
        || output(source::git(&cache).args(["rev-parse", "HEAD"]))?.trim() != revision
    {
        return Err("Espressif cache must be its own checkout at selected revision".into());
    }
    run(source::git(&cache).args(["diff", "--quiet", "--ignore-submodules=all", "HEAD"]))?;
    fs::create_dir_all(destination.parent().ok_or("HAL destination parent")?)
        .map_err(|e| e.to_string())?;
    source::copy_tree(&cache, destination)?;
    let destination = destination.canonicalize().map_err(|e| e.to_string())?;
    if output(source::git(&destination).args(["rev-parse", "--show-toplevel"]))?.trim()
        != destination.to_str().ok_or("HAL path UTF-8")?
        || output(source::git(&destination).args(["rev-parse", "--absolute-git-dir"]))?.trim()
            != destination.join(".git").to_str().ok_or("HAL path UTF-8")?
    {
        return Err("copied HAL Git metadata escapes build checkout".into());
    }
    run(source::git(&destination).args(["clean", "-fdx"]))?;
    let modules = output(source::git(&destination).args(["submodule", "status", "--recursive"]))?;
    for line in modules.lines() {
        let path = line
            .split_whitespace()
            .nth(1)
            .ok_or("invalid submodule status")?;
        let module = destination.join(path);
        let metadata = PathBuf::from(
            output(source::git(&module).args(["rev-parse", "--absolute-git-dir"]))?.trim(),
        );
        if !metadata
            .canonicalize()
            .map_err(|e| e.to_string())?
            .starts_with(destination.join(".git"))
            || !module
                .canonicalize()
                .map_err(|e| e.to_string())?
                .starts_with(&destination)
            || output(source::git(&module).args(["rev-parse", "--show-toplevel"]))?.trim()
                != module.to_str().ok_or("HAL module path UTF-8")?
        {
            return Err("copied submodule Git metadata escapes build checkout".into());
        }
        run(source::git(&module).args(["clean", "-fdx"]))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    #[test]
    fn copied_hal_cache_cannot_clean_the_original_worktree() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let cache = temp.0.join("cache");
        fs::create_dir(&cache).unwrap();
        run(source::git(&cache).args(["init", "--quiet"])).unwrap();
        fs::write(cache.join("tracked"), "source").unwrap();
        run(source::git(&cache).args(["add", "tracked"])).unwrap();
        run(source::git(&cache).args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ]))
        .unwrap();
        let revision = output(source::git(&cache).args(["rev-parse", "HEAD"])).unwrap();
        fs::write(cache.join("keep"), "untracked cache content").unwrap();
        let clean = temp.0.join("clean");
        copy_hal_cache(&cache, &clean, revision.trim()).unwrap();
        assert!(!clean.join("keep").exists());
        assert!(cache.join("keep").exists());
        run(source::git(&cache)
            .args(["config", "core.worktree"])
            .arg(&cache))
        .unwrap();
        let error = copy_hal_cache(&cache, &temp.0.join("escaped"), revision.trim()).unwrap_err();
        assert!(error.contains("escapes build checkout"));
        assert!(cache.join("keep").exists());
    }
    #[test]
    fn literal_config_and_resolution() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let p = temp.0.join(".config");
        fs::write(
            &p,
            "CONFIG_INIT_ARGS=\"old\"\n# CONFIG_FOO is not set\nCONFIG_KEEP=y\n",
        )
        .unwrap();
        let values = BTreeMap::from([
            ("INIT_ARGS".into(), "\"\\\"-a\\\", \\\"b\\\"\"".into()),
            ("FOO".into(), "y".into()),
        ]);
        configure(&p, &values).unwrap();
        let config = read(&p).unwrap();
        assert!(config.contains(&format!("CONFIG_INIT_ARGS={}\n", values["INIT_ARGS"])));
        assert!(config.contains("CONFIG_KEEP=y"));
        required(&config, &["CONFIG_FOO=y".into()], "^CONFIG_UNSAFE=y$").unwrap();
        assert!(required(&config, &[], "^CONFIG_FOO=y$").is_err());
        assert!(required(&config, &["CONFIG_MISSING=y".into()], "").is_err());
    }
    #[test]
    fn helper_headers_cannot_conflict() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let app = temp.0.join("app");
        fs::create_dir(&app).unwrap();
        let mut sources = vec![];
        for (name, header) in [("a", "one"), ("b", "two")] {
            let dir = temp.0.join(name);
            fs::create_dir(&dir).unwrap();
            let source = dir.join("helper.c");
            fs::write(&source, "").unwrap();
            fs::write(dir.join("abi.h"), header).unwrap();
            sources.push(source);
        }
        assert!(stage_helpers(&sources, &app)
            .unwrap_err()
            .contains("Conflicting target helper header"));
    }
}

#[cfg(test)]
mod rebuild_tests {
    use super::*;
    #[test]
    fn failed_rebuild_cannot_reuse_success_evidence() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        for name in [
            ".nxrs-build.json",
            "binaries.sha256",
            "provenance.txt",
            "std-source.json",
            "provider-selection.json",
            "resolved.config",
            "operator-key.pem",
        ] {
            fs::write(temp.0.join(name), "old").unwrap();
        }
        fs::create_dir(temp.0.join("nuttx")).unwrap();
        reset_output(&temp.0).unwrap();
        for name in [
            "binaries.sha256",
            "provenance.txt",
            "std-source.json",
            "provider-selection.json",
            "resolved.config",
            "nuttx",
        ] {
            assert!(!temp.0.join(name).exists());
        }
        assert!(temp.0.join(".nxrs-build.json").is_file());
        assert!(temp.0.join("operator-key.pem").is_file());
    }
}

#[cfg(test)]
mod tool_bundle_tests {
    use super::*;
    #[test]
    fn compiler_discovery_keeps_target_binutils_out_of_host_path() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let bin = temp.0.join("gcc/release/bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join("xtensa-esp-elf-gcc"), "fixture").unwrap();
        let internal = temp.0.join("gcc/release/xtensa-esp-elf/bin");
        fs::create_dir_all(&internal).unwrap();
        fs::write(internal.join("as"), "target-only assembler").unwrap();
        let mut paths = vec![];
        bin_directories(&temp.0.join("gcc"), 6, "xtensa-esp-elf-gcc", &mut paths).unwrap();
        assert_eq!(paths.as_slice(), std::slice::from_ref(&bin));
        let path = env::join_paths(paths).unwrap();
        assert_eq!(
            crate::find_program_on_path("xtensa-esp-elf-gcc", &path).unwrap(),
            bin.join("xtensa-esp-elf-gcc")
        );
    }
}

#[cfg(all(test, unix))]
mod evidence_symlink_tests {
    use super::*;
    #[test]
    fn rebuild_unlinks_evidence_symlinks_before_writing() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let out = temp.0.join("out");
        fs::create_dir(&out).unwrap();
        let outside = temp.0.join("outside");
        fs::write(&outside, "preserved").unwrap();
        std::os::unix::fs::symlink(&outside, out.join("provenance.txt")).unwrap();
        reset_output(&out).unwrap();
        fs::write(out.join("provenance.txt"), "new evidence").unwrap();
        assert_eq!(read(&outside).unwrap(), "preserved");
    }
}
