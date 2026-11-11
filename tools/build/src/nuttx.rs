//! NuttX build sequencing. Upstream Make/Kconfig and compilers remain external tools.
use crate::{
    boot,
    build::{self, App, Options},
    deployment, find_program, hash, output,
    profile::Profile,
    read, reuse, run, sdk, source, write_json, Result,
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
fn iram_layout_source(root: &Path, profile: &Profile) -> Result<Option<PathBuf>> {
    let Some(layout) = profile.data.get("iram-layout") else {
        return Ok(None);
    };
    if profile.data["target"] != "xtensa-esp32s3-nuttx" {
        return Err("iram-layout requires an ESP32-S3 target".into());
    }
    let layout = layout
        .as_str()
        .ok_or("iram-layout must name a linker fragment")?;
    let directory = root
        .join("platform/nuttx/linker")
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let source = root
        .join(layout)
        .canonicalize()
        .map_err(|e| e.to_string())?;
    if !source.starts_with(&directory) || source.extension().is_none_or(|x| x != "ld") {
        return Err("iram-layout must stay under platform/nuttx/linker".into());
    }
    Ok(Some(source))
}
fn stage_iram_layout(source: &Path, nuttx: &Path) -> Result<Vec<PathBuf>> {
    let destination = nuttx.join("boards/xtensa/esp32s3/common/scripts/product_iram.ld");
    if !destination.is_file() {
        return Err("NuttX is missing the product IRAM linker hook".into());
    }
    reuse::copy_if_changed(source, &destination)?;
    Ok(vec![source.to_path_buf(), destination])
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
const ESP32S3_KERNEL_OFFSET: u64 = 0x10000;
const ESP32S3_KERNEL_SIZE: u64 = 0x80000;
// Match the pinned ESP32-S3 PREBUILD formula before compiling Rust helpers.
const ESP32S3_USERSPACE: u64 = 0x3c000030 + ESP32S3_KERNEL_SIZE;

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

fn protected_images(out: &Path, nuttx: &Path, config: &str) -> Result<Vec<PathBuf>> {
    let flash_mib = [4u64, 8, 16, 32]
        .into_iter()
        .find(|size| {
            let flag = format!("CONFIG_ESP32S3_FLASH_{size}M=y");
            config.lines().any(|line| line == flag)
        })
        .ok_or("unsupported protected ESP32-S3 flash size")?;
    let user_offset = ESP32S3_KERNEL_OFFSET + ESP32S3_KERNEL_SIZE;
    let mut regions = Vec::new();
    for (file, offset, limit) in [
        ("nuttx.bin", ESP32S3_KERNEL_OFFSET, user_offset),
        ("nuttx_user.bin", user_offset, flash_mib * 1024 * 1024),
    ] {
        let path = nuttx.join(file);
        let bytes = path
            .metadata()
            .map_err(|e| format!("{}: {e}", path.display()))?
            .len();
        if bytes == 0 || bytes > limit - offset {
            return Err(format!("{file} does not fit its protected flash region"));
        }
        regions.push(json!({"file":format!("nuttx/{file}"),"offset":offset,"size":bytes,"sha256":hash(&path)?}));
    }
    let receipt = out.join("protected-images.json");
    write_json(
        &receipt,
        &json!({"schema":1,"format":"esp32s3-legacy","flash_size":flash_mib * 1024 * 1024,"regions":regions}),
    )?;
    Ok(vec![
        nuttx.join("nuttx_user.elf"),
        nuttx.join("nuttx_user.bin"),
        receipt,
    ])
}
fn helper_identity(source: &Path) -> Result<Value> {
    let source = source.canonicalize().map_err(|e| e.to_string())?;
    let mut headers = BTreeMap::new();
    for entry in fs::read_dir(source.parent().ok_or("helper directory missing")?)
        .map_err(|e| e.to_string())?
    {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.extension().is_some_and(|extension| extension == "h") {
            headers.insert(
                path.file_name()
                    .ok_or("header filename missing")?
                    .to_string_lossy()
                    .into_owned(),
                hash(&path)?,
            );
        }
    }
    Ok(json!({"path":source,"sha256":hash(&source)?,"headers":headers}))
}

// These are the inherited inputs used by Make and its native compiler tools.
// Keep this list explicit: unrelated process variables are not build inputs,
// and override values can contain credentials, so retain only their digests.
const NATIVE_BUILD_ENV: &[&str] = &[
    "NXRS_TARGET_C_FLAGS",
    "EXTRAFLAGS",
    "MAKEFLAGS",
    "MFLAGS",
    "GNUMAKEFLAGS",
    "MAKEOVERRIDES",
    "CC",
    "CXX",
    "CPP",
    "AS",
    "AR",
    "LD",
    "NM",
    "OBJCOPY",
    "STRIP",
    "CFLAGS",
    "CXXFLAGS",
    "CPPFLAGS",
    "AFLAGS",
    "LDFLAGS",
    "HOSTCC",
    "HOSTCXX",
    "HOSTCFLAGS",
    "HOSTCXXFLAGS",
    "HOSTLDFLAGS",
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "LIBRARY_PATH",
    "GCC_EXEC_PREFIX",
    "COMPILER_PATH",
    "SOURCE_DATE_EPOCH",
];

fn native_environment_identity(
    radio: &str,
    get: impl Fn(&str) -> Option<std::ffi::OsString>,
) -> Value {
    let radio_release = (!radio.is_empty()).then(|| {
        get("NXRS_PICO_RADIO_RELEASE")
            .and_then(|value| value.into_string().ok())
            .unwrap_or_else(|| "pico-sdk-2.3.1".into())
    });
    let overrides: BTreeMap<_, _> = NATIVE_BUILD_ENV
        .iter()
        .filter_map(|name| {
            get(name).map(|value| (*name, crate::hash_bytes(value.as_encoded_bytes())))
        })
        .collect();
    json!({"radio_release":radio_release,"native_environment_sha256":overrides})
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
        reuse::copy_if_changed(&source, &app.join(&name))?;
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
                reuse::copy_if_changed(&header, &to)?;
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
    nxrs_build_support::nuttx_resources::validate_overrides(
        &p.data,
        &options.enable,
        &options.disable,
    )?;
    let mut deployment = deployment::validate(root, &app.manifest, p, "nuttx")?;
    let protected = deployment["nuttx-build-mode"] == "protected";
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
                    "protected-runtime",
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
    if !["active", "minimal"].contains(&abi) || (protected && abi != "active") {
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
    let standard = standard(p, protected);
    requirements.extend(standard.iter().map(|(k, v)| line(k, v)));
    if protected {
        let mode = BTreeMap::<String, String>::from([
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
        ]);
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
    let mut sources: Vec<_> = options
        .helpers
        .iter()
        .map(|path| {
            if path.is_absolute() {
                path.clone()
            } else {
                root.join(path)
            }
        })
        .collect();
    for path in &sources {
        if !path.is_file() {
            return Err(format!("missing native helper: {}", path.display()));
        }
    }
    let iram_layout = iram_layout_source(root, p)?;
    let out = build::prepare_output(root, out)?;
    let output_lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(out.join(".nxrs-build.json"))
        .map_err(|error| error.to_string())?;
    output_lock.try_lock().map_err(|error| {
        format!("NuttX build output {}: {error}", out.display())
    })?;
    for input in std::iter::once(&app.manifest)
        .chain(sources.iter())
        .chain(iram_layout.iter())
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
    let original_sysroot = PathBuf::from(
        output(
            tools
                .command(tools.rustc.to_str().ok_or("compiler path UTF-8")?)
                .args(["--print", "sysroot"]),
        )?
        .trim(),
    );
    let shared_sysroot = env::var_os("NXRS_SHARED_NUTTX_STD_SYSROOT").map(PathBuf::from);
    let sdk_input = shared_sysroot.as_deref().unwrap_or(&original_sysroot);
    let radio = match p.data.get("radio-firmware") {
        None => "",
        Some(value) => value.as_str().ok_or("radio-firmware must be a string")?,
    };
    let helper_inputs = sources
        .iter()
        .map(|path| helper_identity(path))
        .collect::<Result<Vec<_>>>()?;
    let cache_inputs = json!({
        "profile":p.data,"profile_input_fingerprint":p.input_fingerprint(),
        "configuration":values,"application":app.manifest,
        "iram_layout":iram_layout.as_ref().map(|source| hash(source)).transpose()?,
        "entry":[app.command.clone(),app.priority.to_string(),app.stack.to_string()],
        "features":options.features,"no_default_features":options.no_default_features,
        "size":size,"trace":trace,"immediate":immediate,"abi":abi,
        "nuttx":source::revision(root,"nuttx")?,"apps":source::revision(root,"nuttx-apps")?,
        "build_inputs":reuse::snapshot(root,&["upstream","tools/build/nuttx/std-app"])?,
        "sdk":sdk_input,"sdk_inputs":reuse::sdk_snapshot(sdk_input)?,
        "compiler":hash(&tools.rustc)?,"native_compiler":hash(&tools.program(&format!("{}gcc",tools.prefix))?)?,
        "controller":hash(&env::current_exe().map_err(|e|e.to_string())?)?,"helpers":helper_inputs,
        "hal_cache":env::var_os("NXRS_ESP_HAL_3RDPARTY_CACHE"),
        "hal_revision":env::var_os("NXRS_ESP_HAL_3RDPARTY_REVISION"),
        "diagnostic_unwind":env::var_os("NXRS_NUTTX_DIAGNOSTIC_UNWIND"),
        "native_environment":native_environment_identity(radio, |name| env::var_os(name))
    });
    let mut cache_directories = vec!["nuttx", "apps", ".nxrs-link-cache"];
    if shared_sysroot.is_none() {
        cache_directories.push("toolchain");
    }
    let retained = cache_paths(&out, &cache_directories)?;
    let reused = reuse::begin(
        &out,
        &cache_inputs,
        &retained.iter().map(String::as_str).collect::<Vec<_>>(),
        options.clean,
    )?;
    if reused {
        clear_success_evidence(&out)?;
    } else {
        reset_output(&out)?;
    }
    let nuttx = out.join("nuttx");
    let apps = out.join("apps");
    if !reused {
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
    }
    let iram_inputs = match iram_layout {
        Some(source) => stage_iram_layout(&source, &nuttx)?,
        None => vec![],
    };
    let entry = apps.join("examples/nxrs_std_app");
    fs::create_dir_all(&entry).map_err(|e| e.to_string())?;
    for file in fs::read_dir(root.join("tools/build/nuttx/std-app")).map_err(|e| e.to_string())? {
        let file = file.map_err(|e| e.to_string())?;
        reuse::copy_if_changed(&file.path(), &entry.join(file.file_name()))?;
    }
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
    if !reused {
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
    }
    let config = read(&nuttx.join(".config"))?;
    required(&config, &requirements, forbid)?;
    nxrs_build_support::nuttx_resources::validate_config(&p.data, &config)?;
    if protected
        && config
            .lines()
            .any(|line| line == "CONFIG_TLS_GLOBAL_KEYS=y")
    {
        return Err("protected direct-init qualification requires group-local pthread keys".into());
    }
    if config.lines().any(|line| line == "CONFIG_ETC_ROMFS=y") {
        tools.program("genromfs")?;
    }
    if config.lines().any(|s| s == "CONFIG_SIG_DEFAULT=y") {
        return Err("unqualified SIG_DEFAULT configuration".into());
    }
    // The pinned hook would rewrite the userspace address already resolved above.
    let prebuild = protected.then_some("PREBUILD=");
    run(tools
        .command("make")
        .current_dir(&nuttx)
        .args([
            "include/nuttx/config.h",
            &format!("CROSSDEV={}", tools.prefix),
        ])
        .args(prebuild))?;
    if protected {
        required(
            &read(&nuttx.join("include/nuttx/config.h"))?,
            &[format!(
                "#define CONFIG_NUTTX_USERSPACE {ESP32S3_USERSPACE:#x}"
            )],
            "",
        )?;
    }
    fs::copy(nuttx.join(".config"), out.join("resolved.config")).map_err(|e| e.to_string())?;
    write_json(&out.join("provider-selection.json"), &deployment)?;
    let sysroot = if let Some(shared) = shared_sysroot {
        let shared = shared.canonicalize().map_err(|e| e.to_string())?;
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
    } else if reused {
        out.join("toolchain")
    } else {
        sdk::prepare(root, &original_sysroot, &out, tools.sdk)?
    };
    sources.extend(sdk::native_sources(&sysroot, &out.join("std-patch.json"))?);
    let (sources, objects) = stage_helpers(&sources, &entry)?;
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
        reuse::write_if_changed(&out.join("xtensa-bare-target.json"), bare.as_bytes())?;
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
        reuse::write_if_changed(
            &target_arg,
            serde_json::to_string_pretty(&spec)
                .map_err(|e| e.to_string())?
                .as_bytes(),
        )?;
        let downloads = env::var_os("NXRS_QEMU_TOOLS_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("target/qemu-tools"))
            .join("downloads.sha256");
        fs::copy(downloads, out.join("downloads.sha256")).map_err(|e| e.to_string())?;
    }
    let target_dir = env::var_os("NXRS_SHARED_CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| out.join("cargo"));
    let elf = target_dir.join(target).join("release").join(&app.bin);
    if !reused {
        // A shared Cargo directory may still contain a fresh executable after a
        // failed native build. Require a new root link when its evidence was reset.
        crate::remove_file(&elf)?;
    }
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
    make_args.extend(prebuild.map(str::to_owned));
    if tools.esp {
        make_args.push("ESPTOOL_BINDIR=.".into());
        if !reused {
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
    }
    run(tools
        .command("make")
        .arg("-C")
        .arg(&nuttx)
        .arg("context")
        .args(&make_args))?;
    if !reused
        && signed
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
    if !reused && tools.esp {
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
    let native_config = native_context(&tools, &nuttx, &entry, &out, target, &make_args)?;
    let linker = out.join("nxrs-nuttx-link");
    reuse::copy_if_changed(&env::current_exe().map_err(|e| e.to_string())?, &linker)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&linker, fs::Permissions::from_mode(0o755))
            .map_err(|e| e.to_string())?;
    }
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
        .arg(dunce::simplified(&app.manifest))
        .args(["-p", &app.package, "--bin", &app.bin, "--target"])
        .arg(&target_arg)
        .args([
            "-Zbuild-std=std,panic_abort",
            "--message-format=json-render-diagnostics",
        ])
        .env("NUTTX_STD_SYSROOT", &sysroot)
        .env("RUSTC", &rustc)
        .env("CARGO_BUILD_RUSTC", &rustc)
        .env("CARGO_TARGET_DIR", &target_dir)
        .env("NXRS_NUTTX_INCLUDE", nuttx.join("include"))
        .env("NXRS_NUTTX_NATIVE_CONFIG", &native_config)
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
    build::configure_profile(&mut cargo, &out, root, p, target, "nuttx")?;
    options.configure_cargo(&mut cargo, &[]);
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
    let selection = crate::cargo_graph::resolve(&mut cargo, p)?;
    deployment["configuration"] = json!(
        build::configure_preparation(&mut cargo, &out, p, &selection, target, "nuttx")?
    );
    let native_resources = out.join("native-resources");
    use nxrs_build_support::prepared_resource;
    let resources = if env::var_os("NXRS_CARGO_CONFIG").is_some()
        || prepared_resource::cargo_compiler_environment(root)?
    {
        Vec::new()
    } else {
        prepare_native_resources(
            &selection, p, target, &native_config, &native_resources, &tools.path,
        )?
    };
    let prepared = prepared_resource::ENVIRONMENT;
    if resources.is_empty() {
        cargo.env_remove(prepared);
    } else {
        cargo.env(prepared, &native_resources);
    }
    deployment["native-resources"] = json!(resources);
    deployment["dependency_resolution"] = selection.json();
    deployment["model_features"] = json!(selection.model_features);
    deployment["driver_features"] = json!(selection.driver_features);
    let editor_sdk: BTreeMap<_, _> = ["NXRS_PICO_SDK", "NXRS_TLS_ARCHIVE"]
        .into_iter()
        .filter_map(|name| env::var_os(name).map(|path| (name, PathBuf::from(path))))
        .collect();
    let editor = json!({
        "cargo": tools.cargo,
        "features": options.features.iter()
            .chain(&selection.driver_features)
            .chain(&selection.model_features)
            .collect::<Vec<_>>(),
        "no_default_features": options.no_default_features,
        "sdk": editor_sdk,
    });
    write_json(&out.join("provider-selection.json"), &deployment)?;
    write_json(&out.join(".editor-cargo.json"), &editor)?;
    build::cargo_messages(&mut cargo, &out.join("cargo-messages.jsonl"))?;
    restore_link_evidence(&out, &elf)?;
    build::std_source(&out.join("cargo-messages.jsonl"), &sysroot, &rustc, &out)?;
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
    let config = read(&nuttx.join(".config"))?;
    if protected {
        required(&config, &requirements, forbid)?;
        nxrs_build_support::nuttx_resources::validate_config(&p.data, &config)?;
    }
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
    require_symbols(&symbols, &[("nx_start", "TtWw")])?;
    let user_symbols = if protected {
        if symbols
            .lines()
            .any(|line| line.split_whitespace().last() == Some(entry_symbol.as_str()))
        {
            return Err("protected application entry is present in the kernel ELF".into());
        }
        let user_symbols = output(
            tools
                .command(&format!("{}nm", tools.prefix))
                .arg("--defined-only")
                .arg(nuttx.join("nuttx_user.elf")),
        )?;
        Some(user_symbols)
    } else {
        None
    };
    let mut needed = vec![(entry_symbol.as_str(), "TtWw")];
    if abi == "active" {
        needed.extend([
            ("pthread_create", "TtWw"),
            ("pthread_join", "TtWw"),
            ("clock_gettime", "TtWw"),
        ]);
    }
    require_symbols(user_symbols.as_deref().unwrap_or(&symbols), &needed)?;
    if let Some(user_symbols) = user_symbols {
        fs::write(out.join("user-symbols.txt"), user_symbols).map_err(|e| e.to_string())?;
    }
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
        format!(
            "nuttx_build_mode={}",
            deployment["nuttx-build-mode"].as_str().unwrap()
        ),
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
        elf.clone(),
        app.manifest.clone(),
        out.join("resolved.config"),
        out.join("provider-selection.json"),
    ];
    if protected {
        inputs.extend(protected_images(&out, &nuttx, &config)?);
    }
    inputs.extend(p.source_paths().cloned());
    inputs.extend(iram_inputs);
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
    save_link_evidence(&out, &elf)?;
    let retained = cache_paths(&out, &cache_directories)?;
    reuse::complete(
        &out,
        &cache_inputs,
        &retained.iter().map(String::as_str).collect::<Vec<_>>(),
    )?;
    if protected {
        println!(
            "NuttX protected image pair: {}",
            out.join("protected-images.json").display()
        );
    } else {
        println!("NuttX image: {}", image.display());
    }
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
fn cache_paths(out: &Path, directories: &[&str]) -> Result<Vec<String>> {
    let mut paths: Vec<_> = directories.iter().map(|name| (*name).to_owned()).collect();
    for entry in fs::read_dir(out).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| "build input name UTF-8")?;
        if name == "std-patch.json" || name.ends_with("-patches.json") || name.ends_with(".patch") {
            paths.push(name);
        }
    }
    paths.sort();
    Ok(paths)
}

fn prepare_native_resources(
    selection: &crate::cargo_graph::Selection,
    profile: &Profile,
    target: &str,
    context: &Path,
    out: &Path,
    compiler_path: &std::ffi::OsStr,
) -> Result<Vec<Value>> {
    use nxrs_build_support::prepared_resource;
    let mut prepared = Vec::new();
    let mut directories = std::collections::BTreeSet::new();
    for (package, directory, features) in selection.path_packages() {
        if !prepared_resource::selected(directory, profile, features)? {
            continue;
        }
        let archive = prepared_resource::prepare(directory, target, context, out, compiler_path)?;
        directories.insert(directory.canonicalize().map_err(|error| error.to_string())?);
        prepared.push(json!({"package": package, "sha256": hash(&archive)?, "archive": archive}));
    }
    if !directories.is_empty() {
        let path = out.join(prepared_resource::PACKAGE_LIST);
        let bytes = serde_json::to_vec(&directories).map_err(|error| error.to_string())?;
        if path.is_symlink() || fs::read(&path).ok().as_deref() != Some(bytes.as_slice()) {
            let temporary = crate::TempDir::new(out)?;
            let candidate = temporary.0.join(prepared_resource::PACKAGE_LIST);
            fs::write(&candidate, bytes).map_err(|error| error.to_string())?;
            fs::rename(candidate, path).map_err(|error| error.to_string())?;
        }
    }
    Ok(prepared)
}

/// Capture Make's selected ABI rather than maintain a second set of C flags.
fn native_context(
    tools: &Tools,
    nuttx: &Path,
    entry: &Path,
    out: &Path,
    target: &str,
    make_args: &[String],
) -> Result<PathBuf> {
    let temporary = crate::TempDir::new(out)?;
    run(tools
        .command("make")
        .arg("--no-print-directory")
        .arg("-C")
        .arg(entry)
        .arg("nxrs-native-context")
        .args(make_args)
        .arg(format!("TOPDIR={}", nuttx.display()))
        .arg(format!("APPDIR={}", out.join("apps").display()))
        .arg(format!("NXRS_NATIVE_CONTEXT_DIR={}", temporary.0.display())))?;
    let words = |name: &str| crate::split_flags(&read(&temporary.0.join(format!("{name}.txt")))?);
    let mut cc = words("cc")?;
    let mut ar = words("ar")?;
    for command in [&mut cc, &mut ar] {
        let program = command
            .first_mut()
            .ok_or("empty native tool command from Make")?;
        *program = tools
            .program(program)?
            .to_str()
            .ok_or("native tool path UTF-8")?
            .to_owned();
    }
    let context = json!({"schema":1,"target":target,"directory":entry,
        "include":nuttx.join("include"),"config_sha256":hash(&nuttx.join("include/nuttx/config.h"))?,
        "headers_sha256":crate::hash_bytes(&serde_json::to_vec(&reuse::snapshot(nuttx,&["include"])?).map_err(|e|e.to_string())?),
        "cc":cc,"ar":ar,"cflags":words("cflags")?});
    let path = out.join("native-context.json");
    reuse::write_if_changed(
        &path,
        serde_json::to_string_pretty(&context)
            .map_err(|e| e.to_string())?
            .as_bytes(),
    )?;
    Ok(path)
}

const LINK_EVIDENCE: &[&str] = &[
    "rust-link.json",
    "rust-partial.map",
    "rust-input-before.elf",
    "rust-input.elf",
    "rust-relocations.txt",
    "rust-symbols-before.txt",
    "discarded-undefined.txt",
    "rust-symbol-pruning.json",
];

fn save_link_evidence(out: &Path, elf: &Path) -> Result<()> {
    let cache = out.join(".nxrs-link-cache");
    crate::remove_dir(&cache)?;
    fs::create_dir(&cache).map_err(|e| e.to_string())?;
    for name in LINK_EVIDENCE {
        if out.join(name).is_file() {
            fs::copy(out.join(name), cache.join(name)).map_err(|e| e.to_string())?;
        }
    }
    write_json(&cache.join("artifact.json"), &json!({"sha256":hash(elf)?}))
}

fn restore_link_evidence(out: &Path, elf: &Path) -> Result<()> {
    // A fresh Cargo link writes its own receipt. A fresh Cargo artifact may
    // reuse the previous link only after the completed cache and ELF agree.
    let reused = !out.join("rust-link.json").is_file();
    if reused {
        let cache = out.join(".nxrs-link-cache");
        if crate::read_json(&cache.join("artifact.json"))?["sha256"] != hash(elf)? {
            return Err(
                "fresh Cargo artifact differs from completed linker evidence; rebuild with --clean"
                    .into(),
            );
        }
        for name in LINK_EVIDENCE {
            if cache.join(name).is_file() {
                fs::copy(cache.join(name), out.join(name)).map_err(|e| e.to_string())?;
            }
        }
        if !out.join("rust-link.json").is_file() {
            return Err("missing linker evidence".into());
        }
    }
    write_json(
        &out.join("link-reuse.json"),
        &json!({"reused":reused,"artifact_sha256":hash(elf)?}),
    )
}

fn clear_success_evidence(out: &Path) -> Result<()> {
    for entry in fs::read_dir(out).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let name = entry.file_name();
        let name = name.to_str().ok_or("build output name UTF-8")?;
        // These describe retained configuration/source preparation, not success
        // of this invocation. Generated target/context bytes are written only if changed.
        let input = [
            ".nxrs-build.json",
            "std-patch.json",
            "nuttx-patches.json",
            "nuttx-apps-patches.json",
            "mcuboot-patches.json",
            "esp-hal-patches.json",
            "native-context.json",
            "resolved-profile.json",
            "xtensa-bare-target.json",
            "xtensa-esp32s3-nuttx.json",
        ]
        .contains(&name)
            || name.ends_with(".patch");
        if !input
            && entry.path().extension().is_some_and(|e| {
                ["json", "jsonl", "txt", "sha256", "config"]
                    .iter()
                    .any(|s| e == *s)
            })
        {
            crate::remove_file(&entry.path())?;
        }
    }
    for name in [
        "provider-selection.json",
        "cargo-messages.jsonl",
        "std-source.json",
        "resolved.config",
        "rust-elf-header.txt",
        "rust-symbols.txt",
        "symbols.txt",
        "image-size.txt",
        "image-layout.txt",
        "provenance.txt",
        "binaries.sha256",
        "abi-report.json",
        "abi-native.c",
        "abi-rust.rs",
        "abi-native.o",
        "abi-rust.o",
        "abi-native.bin",
        "abi-rust.bin",
        "link-reuse.json",
    ]
    .into_iter()
    .chain(LINK_EVIDENCE.iter().copied())
    {
        crate::remove_file(&out.join(name))?;
    }
    Ok(())
}

fn reset_output(out: &Path) -> Result<()> {
    for name in ["nuttx", "apps", "cargo", "toolchain", "configuration", "native-resources", ".nxrs-link-cache"] {
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
    fn protected_image_pair_records_both_regions_and_rejects_overlap() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let out = &temp.0;
        let nuttx = out.join("nuttx");
        fs::create_dir(&nuttx).unwrap();
        let kernel = nuttx.join("nuttx.bin");
        let user = nuttx.join("nuttx_user.bin");
        fs::write(&kernel, b"kernel").unwrap();
        let config = "CONFIG_ESP32S3_FLASH_16M=y";
        assert!(protected_images(out, &nuttx, config).is_err());
        fs::write(user, b"userspace").unwrap();
        protected_images(out, &nuttx, config).unwrap();
        let receipt = crate::read_json(&out.join("protected-images.json")).unwrap();
        assert_eq!(receipt["regions"][0]["offset"], 0x10000);
        assert_eq!(receipt["regions"][1]["offset"], 0x90000);
        assert_eq!(receipt["regions"][1]["size"], 9);
        fs::File::create(kernel)
            .unwrap()
            .set_len(ESP32S3_KERNEL_SIZE + 1)
            .unwrap();
        assert!(protected_images(out, &nuttx, config).is_err());
    }
    #[test]
    fn product_iram_layout_is_staged_and_recorded_without_app_wiring() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let layouts = temp.0.join("platform/nuttx/linker");
        let native = temp.0.join("nuttx");
        let staged = native.join("boards/xtensa/esp32s3/common/scripts/product_iram.ld");
        fs::create_dir_all(&layouts).unwrap();
        fs::create_dir_all(staged.parent().unwrap()).unwrap();
        fs::write(&staged, "/* default */").unwrap();
        let source = layouts.join("sync.ld");
        fs::write(&source, "*(.text.sync .literal.sync)\n").unwrap();
        let mut profile = Profile::from_data(json!({"target":"xtensa-esp32s3-nuttx"}), vec![]);
        assert!(iram_layout_source(&temp.0, &profile).unwrap().is_none());
        profile.data["iram-layout"] = json!("platform/nuttx/linker/sync.ld");
        let selected = iram_layout_source(&temp.0, &profile).unwrap().unwrap();
        let inputs = stage_iram_layout(&selected, &native).unwrap();
        assert_eq!(inputs, [source.canonicalize().unwrap(), staged.clone()]);
        assert_eq!(read(&staged).unwrap(), read(&source).unwrap());
        profile.data["target"] = json!("thumbv7em-nuttx-eabihf");
        assert!(iram_layout_source(&temp.0, &profile).is_err());
        profile.data["target"] = json!("xtensa-esp32s3-nuttx");
        fs::write(temp.0.join("outside.ld"), "outside").unwrap();
        profile.data["iram-layout"] = json!("outside.ld");
        assert!(iram_layout_source(&temp.0, &profile).is_err());
        assert_eq!(read(&staged).unwrap(), read(&source).unwrap());
    }
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
    fn radio_release_and_native_overrides_discard_warm_configuration() {
        let first = native_environment_identity("cyw43439-bluetooth", |_| None);
        let explicit_default = native_environment_identity("cyw43439-bluetooth", |name| {
            (name == "NXRS_PICO_RADIO_RELEASE").then(|| "pico-sdk-2.3.1".into())
        });
        assert_eq!(first, explicit_default);
        for (name, value) in [
            ("NXRS_PICO_RADIO_RELEASE", "pico-sdk-2.1.1"),
            ("NXRS_TARGET_C_FLAGS", "-DHELPER_ABI=2"),
            ("EXTRAFLAGS", "-DNATIVE_ABI=2"),
            ("MAKEFLAGS", "CC=other-cc"),
            ("MFLAGS", "-e"),
            ("GNUMAKEFLAGS", "CFLAGS=-O0"),
            ("MAKEOVERRIDES", "EXTRAFLAGS=-DNATIVE_ABI=2"),
            ("CC", "other-cc"),
            ("CFLAGS", "-DNATIVE_ABI=2"),
            ("C_INCLUDE_PATH", "/other/headers"),
        ] {
            let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
            fs::create_dir(temp.0.join("nuttx")).unwrap();
            fs::create_dir(temp.0.join("apps")).unwrap();
            fs::write(temp.0.join("nuttx/.config"), "previous radio asset path").unwrap();
            fs::write(temp.0.join("apps/helper.o"), "previous compile flags").unwrap();
            reuse::complete(&temp.0, &first, &["nuttx", "apps"]).unwrap();
            assert!(reuse::begin(&temp.0, &explicit_default, &["nuttx", "apps"], false).unwrap());
            reuse::complete(&temp.0, &first, &["nuttx", "apps"]).unwrap();

            let changed = native_environment_identity("cyw43439-bluetooth", |key| {
                (key == name).then(|| value.into())
            });
            assert!(
                !reuse::begin(&temp.0, &changed, &["nuttx", "apps"], false).unwrap(),
                "changed {name} retained native configuration"
            );
            assert!(!temp.0.join("nuttx/.config").exists());
            assert!(!temp.0.join("apps/helper.o").exists());
        }
    }

    #[test]
    fn native_identity_excludes_unrelated_environment_and_plaintext_overrides() {
        let first = native_environment_identity("", |_| None);
        assert_eq!(
            first,
            native_environment_identity("", |name| {
                matches!(name, "NXRS_PICO_RADIO_RELEASE" | "UNRELATED_TOKEN")
                    .then(|| "unused value".into())
            })
        );
        let identity = native_environment_identity("", |name| {
            (name == "MAKEFLAGS").then(|| "CFLAGS=-DPRIVATE_VALUE=example_secret".into())
        });
        assert_ne!(identity, first);
        assert!(!identity.to_string().contains("example_secret"));
    }

    #[test]
    fn helper_header_edits_and_deletions_reset_staged_inputs() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let source = temp.0.join("source");
        let out = temp.0.join("out");
        fs::create_dir(&source).unwrap();
        fs::create_dir(&out).unwrap();
        let helper = source.join("helper.c");
        let header = source.join("abi.h");
        fs::write(&helper, "#include \"abi.h\"\n").unwrap();
        fs::write(&header, "#define ABI 1\n").unwrap();
        let entry = out.join("apps");
        fs::create_dir(&entry).unwrap();
        stage_helpers(std::slice::from_ref(&helper), &entry).unwrap();
        let first = helper_identity(&helper).unwrap();
        reuse::complete(&out, &first, &["apps"]).unwrap();
        assert!(reuse::begin(&out, &first, &["apps"], false).unwrap());
        reuse::complete(&out, &first, &["apps"]).unwrap();

        // A warm build must replace the staged header, not report a conflict.
        fs::write(&header, "#define ABI 2\n").unwrap();
        let edited = helper_identity(&helper).unwrap();
        assert!(!reuse::begin(&out, &edited, &["apps"], false).unwrap());
        fs::create_dir(&entry).unwrap();
        stage_helpers(std::slice::from_ref(&helper), &entry).unwrap();
        assert_eq!(
            fs::read(entry.join("abi.h")).unwrap(),
            fs::read(&header).unwrap()
        );
        reuse::complete(&out, &edited, &["apps"]).unwrap();

        // Removing a header must remove its old staged copy as on a clean build.
        fs::remove_file(&header).unwrap();
        let removed = helper_identity(&helper).unwrap();
        assert!(!reuse::begin(&out, &removed, &["apps"], false).unwrap());
        fs::create_dir(&entry).unwrap();
        stage_helpers(std::slice::from_ref(&helper), &entry).unwrap();
        assert!(!entry.join("abi.h").exists());
    }

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

    #[test]
    fn incremental_builds_preserve_objects_but_recheck_link_evidence() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let out = &temp.0;
        fs::create_dir(out.join("nuttx")).unwrap();
        fs::write(out.join("nuttx/retained.o"), "native object").unwrap();
        fs::write(out.join("std-patch.json"), "SDK input receipt").unwrap();
        fs::write(out.join("abi-report.json"), "old success").unwrap();
        fs::write(out.join("binaries.sha256"), "old image digest").unwrap();
        let elf = out.join("application");
        fs::write(&elf, "same Rust artifact").unwrap();
        fs::write(out.join("rust-link.json"), "link invocation").unwrap();
        save_link_evidence(out, &elf).unwrap();
        clear_success_evidence(out).unwrap();
        assert!(out.join("nuttx/retained.o").is_file());
        assert!(out.join("std-patch.json").is_file());
        assert!(!out.join("abi-report.json").exists());
        assert!(!out.join("binaries.sha256").exists());
        assert!(!out.join("rust-link.json").exists());
        restore_link_evidence(out, &elf).unwrap();
        assert_eq!(
            crate::read_json(&out.join("link-reuse.json")).unwrap()["reused"],
            true
        );
        clear_success_evidence(out).unwrap();
        fs::write(&elf, "different artifact").unwrap();
        assert!(restore_link_evidence(out, &elf)
            .unwrap_err()
            .contains("differs"));
        assert!(!out.join("rust-link.json").exists());
    }

    #[cfg(unix)]
    #[test]
    fn native_context_uses_the_application_make_compiler_and_flags() {
        let temp = crate::TempDir::new(&env::temp_dir()).unwrap();
        let out = &temp.0;
        let nuttx = out.join("nuttx");
        let apps = out.join("apps");
        let entry = apps.join("examples/nxrs_std_app");
        fs::create_dir_all(nuttx.join("include/nuttx")).unwrap();
        fs::create_dir_all(&entry).unwrap();
        fs::write(nuttx.join("include/nuttx/config.h"), "#define DEVICE 1\n").unwrap();
        fs::write(
            apps.join("Make.defs"),
            "CC = cc\nAR = ar rcs\nCFLAGS = -O2 -DDEVICE_ABI=7 -DQUOTED=\"two words\" -DSINGLE=\"it's literal\" -DDOLLAR='$$literal' -DBACKTICK='`literal`' -DSUBSTITUTION='$$(touch exported-command-ran)'\nINCDIR_PREFIX = -I\n",
        )
        .unwrap();
        fs::write(apps.join("Application.mk"), "").unwrap();
        fs::copy(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("nuttx/std-app/Makefile"),
            entry.join("Makefile"),
        )
        .unwrap();
        let tools = Tools {
            sdk: "fixture",
            cargo: PathBuf::new(),
            rustc: PathBuf::new(),
            prefix: String::new(),
            esp: false,
            path: env::var_os("PATH").unwrap(),
        };
        let path = native_context(&tools, &nuttx, &entry, out, "fixture-nuttx", &[]).unwrap();
        let data = crate::read_json(&path).unwrap();
        assert_eq!(data["target"], "fixture-nuttx");
        assert_eq!(
            data["config_sha256"],
            hash(&nuttx.join("include/nuttx/config.h")).unwrap()
        );
        let flags = data["cflags"].as_array().unwrap();
        assert!(flags.contains(&json!("-DDEVICE_ABI=7")));
        assert!(flags.contains(&json!("-DQUOTED=two words")));
        assert!(flags.contains(&json!("-DSINGLE=it's literal")));
        assert!(flags.contains(&json!("-DDOLLAR=$literal")));
        assert!(flags.contains(&json!("-DBACKTICK=`literal`")));
        assert!(flags.contains(&json!("-DSUBSTITUTION=$(touch exported-command-ran)")));
        assert!(!entry.join("exported-command-ran").exists());
        assert!(flags.contains(&json!(format!("-I{}", nuttx.display()))));
        assert!(Path::new(data["cc"][0].as_str().unwrap()).is_absolute());
        assert!(Path::new(data["ar"][0].as_str().unwrap()).is_absolute());
        assert_eq!(data["ar"][1], "rcs");
        let before = path.metadata().unwrap().modified().unwrap();
        native_context(&tools, &nuttx, &entry, out, "fixture-nuttx", &[]).unwrap();
        assert_eq!(path.metadata().unwrap().modified().unwrap(), before);

        // Make's temporary config file must not defeat otherwise fresh Cargo artifacts.
        let temporary_header = nuttx.join("include/nuttx/config.h.tmp");
        fs::write(&temporary_header, "temporary").unwrap();
        fs::remove_file(&temporary_header).unwrap();
        native_context(&tools, &nuttx, &entry, out, "fixture-nuttx", &[]).unwrap();
        assert_eq!(path.metadata().unwrap().modified().unwrap(), before);

        // New configured headers have no existing Cargo watch. Their inventory
        // changes the context, and removal restores the original content identity.
        let added = nuttx.join("include/nuttx/added.hpp");
        fs::write(&added, "#define ADDED 1\n").unwrap();
        native_context(&tools, &nuttx, &entry, out, "fixture-nuttx", &[]).unwrap();
        assert_ne!(
            crate::read_json(&path).unwrap()["headers_sha256"],
            data["headers_sha256"]
        );
        fs::remove_file(&added).unwrap();
        native_context(&tools, &nuttx, &entry, out, "fixture-nuttx", &[]).unwrap();
        assert_eq!(
            crate::read_json(&path).unwrap()["headers_sha256"],
            data["headers_sha256"]
        );

        // Architecture include directories are symlinks. Retargeting one must
        // invalidate even when the two selected headers have identical bytes.
        for name in ["arch-one", "arch-two"] {
            fs::create_dir(nuttx.join(name)).unwrap();
            fs::write(nuttx.join(name).join("abi.h"), "#define ABI 1\n").unwrap();
        }
        let link = nuttx.join("include/arch");
        std::os::unix::fs::symlink(nuttx.join("arch-one"), &link).unwrap();
        native_context(&tools, &nuttx, &entry, out, "fixture-nuttx", &[]).unwrap();
        let first_arch = crate::read_json(&path).unwrap()["headers_sha256"].clone();
        fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(nuttx.join("arch-two"), &link).unwrap();
        native_context(&tools, &nuttx, &entry, out, "fixture-nuttx", &[]).unwrap();
        assert_ne!(
            crate::read_json(&path).unwrap()["headers_sha256"],
            first_arch
        );
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
