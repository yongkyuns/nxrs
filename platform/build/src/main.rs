//! One host-only entry for Nxrs build orchestration.
#![forbid(unsafe_code)]
use nxrs_build::{
    build::{self, App, Options},
    profile::Profile,
    target::Target,
    Result,
};
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::ExitCode,
};
fn workspace_root() -> Result<PathBuf> {
    let mut directory = env::current_dir().map_err(|e| e.to_string())?;
    loop {
        if directory.join("Cargo.toml").is_file()
            && nxrs_build::read(&directory.join("Cargo.toml"))?
                .lines()
                .any(|s| s.trim() == "[workspace]")
        {
            return directory.canonicalize().map_err(|e| e.to_string());
        }
        if !directory.pop() {
            return Err("cannot locate Nxrs workspace".into());
        }
    }
}
fn usage() -> &'static str {
    "usage:
  cargo nxrs build --app <app> --platform <platform> [--profile <profile>] [--out <path>] [--dry-run]
  cargo nxrs build --manifest-path <path> --profile <qualification-profile> [--bin <bin>]
  cargo nxrs platforms
  cargo nxrs apps
  cargo nxrs profile --platform <profile> [--target-os <os>]
  cargo nxrs fetch <tls|detector>

Platforms: wroom, cores3, pico2w, linux, macos, windows, web.
Use --workspace-root <path> before a command to select an Nxrs checkout.
Run cargo nxrs build --help for build options.
NuttX signed profiles require the existing signing-key, verification-root, version and counter inputs."
}
fn build_usage() -> &'static str {
    "usage:
  cargo nxrs build --app <app> --platform <platform> [options]
  cargo nxrs build --manifest-path <path> --profile <profile> [options]

Selection:
  --app <app>                 Select an application under app/
  --manifest-path <path>      Select a Cargo manifest; --app-manifest is an alias
  --bin <bin>                 Select a binary target
  --platform <platform>       Select a platform family and its default profile
  --profile <profile>         Select a product or qualification profile
  --features <features>       Enable Cargo features, separated by commas or spaces;
                              repeat as needed; --feature is an alias
  --no-default-features       Disable the application's default Cargo features
  --out <path>                Override the build output directory
  --dry-run                   Validate selection and print it without building

Browser builds:
  --html <path>               Supply an HTML template with one __NXRS_SCRIPT__ marker
  --unmodified-browser-std    Use the unpatched browser standard library

NuttX qualification:
  --target-c-source <path>    Add a target C source; repeat as needed
  --kconfig-enable <symbol>   Enable a Kconfig symbol; repeat as needed
  --kconfig-disable <symbol>  Disable a Kconfig symbol; repeat as needed
  --abi-profile <profile>     Select active or minimal ABI checks
  --size-optimized            Enable size qualification settings
  --trace-only-backtrace      Use trace-only backtraces
  --panic-immediate-abort     Abort immediately; requires --trace-only-backtrace
  --app-package <package>     Check the selected package name
  --command <command>         Override the firmware command name
  --priority <priority>       Override the firmware task priority
  --stack-size <bytes>        Override the firmware task stack size

Paths are relative to the selected Nxrs workspace unless absolute.
--profile selects an Nxrs configuration; builds use Cargo's release profile."
}
fn profile_usage() -> &'static str {
    "usage:
  cargo nxrs profile --platform <profile> [--target-os <os>]
  cargo nxrs profile --platform <profile> --capability <capability> --target-os <os>
  cargo nxrs profile --platform <profile> --model-features <manifest-path>
  cargo nxrs profile --platform <profile> --native-helpers

Manifest paths are relative to the selected Nxrs workspace unless absolute."
}
fn applications(root: &Path) -> Result<()> {
    let mut names = vec![];
    for entry in fs::read_dir(root.join("app")).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let directory = entry.path();
        if !fs::metadata(&directory)
            .map_err(|e| format!("{}: {e}", directory.display()))?
            .is_dir()
        {
            continue;
        }
        let manifest = directory.join("Cargo.toml");
        if manifest
            .try_exists()
            .map_err(|e| format!("{}: {e}", manifest.display()))?
        {
            App::load(&manifest, None)
                .map_err(|e| format!("invalid application {}: {e}", manifest.display()))?;
            names.push(entry.file_name());
        }
    }
    names.sort();
    for name in names {
        println!("{}", name.to_string_lossy());
    }
    Ok(())
}
fn build_command(root: &Path, args: &[String]) -> Result<ExitCode> {
    let (mut app, mut manifest, mut platform, mut profile, mut out, mut bin) =
        (None, None, None, None, None, None);
    let mut options = Options::default();
    let mut html = None;
    let mut web_fix = true;
    let mut dry = false;
    let mut args = args.iter();
    let mut expected_package = None;
    let mut command = None;
    let mut priority = None;
    let mut stack = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--html" => html = Some(root.join(args.next().ok_or("missing HTML template")?)),
            "--unmodified-browser-std" => web_fix = false,
            "--dry-run" => dry = true,
            "--size-optimized" => options.size = true,
            "--trace-only-backtrace" => options.trace = true,
            "--panic-immediate-abort" => options.immediate = true,
            "--app" => app = Some(args.next().ok_or("missing --app")?.clone()),
            "--manifest-path" | "--app-manifest" => {
                manifest = Some(PathBuf::from(args.next().ok_or("missing manifest path")?))
            }
            "--platform" => platform = Some(args.next().ok_or("missing --platform")?.clone()),
            "--profile" => profile = Some(args.next().ok_or("missing --profile")?.clone()),
            "--out" => out = Some(root.join(args.next().ok_or("missing output path")?)),
            "--bin" => bin = Some(args.next().ok_or("missing binary")?.clone()),
            "--feature" | "--features" => {
                let list = args.next().ok_or("missing Cargo feature list")?;
                let features: Vec<_> = list
                    .split(|c: char| c == ',' || c.is_whitespace())
                    .filter(|feature| !feature.is_empty())
                    .map(str::to_owned)
                    .collect();
                if features.is_empty() {
                    return Err("missing Cargo feature list".into());
                }
                options.features.extend(features);
            }
            "--no-default-features" => options.no_default_features = true,
            "--target-c-source" => options
                .helpers
                .push(root.join(args.next().ok_or("missing helper source")?)),
            "--kconfig-enable" => options
                .enable
                .push(args.next().ok_or("missing symbol")?.clone()),
            "--kconfig-disable" => options
                .disable
                .push(args.next().ok_or("missing symbol")?.clone()),
            "--abi-profile" => {
                options.abi = Some(args.next().ok_or("missing ABI profile")?.clone())
            }
            "--app-package" => {
                expected_package = Some(args.next().ok_or("missing package")?.clone())
            }
            "--command" => command = Some(args.next().ok_or("missing command")?.clone()),
            "--priority" => {
                priority = Some(
                    args.next()
                        .ok_or("missing priority")?
                        .parse::<u32>()
                        .map_err(|_| "invalid priority")?,
                )
            }
            "--stack-size" => {
                stack = Some(
                    args.next()
                        .ok_or("missing stack size")?
                        .parse::<u32>()
                        .map_err(|_| "invalid stack size")?,
                )
            }
            _ => return Err(format!("unknown build argument {arg}")),
        }
    }
    options.validate()?;
    let path = match (app, manifest) {
        (Some(name), None) if build::name(&name) => root.join("app").join(name).join("Cargo.toml"),
        (None, Some(p)) => {
            if p.is_absolute() {
                p
            } else {
                root.join(p)
            }
        }
        _ => return Err("choose one --app or --manifest-path".into()),
    };
    let mut app = App::load(&path, bin.as_deref())?;
    if expected_package.is_some_and(|s| s != app.package) {
        return Err("requested package differs from selected manifest".into());
    }
    if let Some(s) = command {
        if !build::name(&s) {
            return Err("invalid command".into());
        }
        app.command = s;
    }
    if let Some(n) = priority {
        if n == 0 {
            return Err("priority must be positive".into());
        }
        app.priority = n;
    }
    if let Some(n) = stack {
        if n == 0 {
            return Err("stack size must be positive".into());
        }
        app.stack = n;
    }
    let profile = if let Some(platform) = platform {
        let target = Target::parse(&platform)?;
        build::selection(root, target, profile.as_deref())?
    } else {
        Profile::resolve(
            root,
            profile
                .as_deref()
                .ok_or("choose a --platform or explicit qualification --profile")?,
        )?
    };
    let os = if profile.data.get("board").is_some() {
        "nuttx"
    } else if profile.data["execution"] == "web" {
        "emscripten"
    } else {
        env::consts::OS
    };
    options.validate_for_backend(os)?;
    if os != "emscripten" && (html.is_some() || !web_fix) {
        return Err("browser switches require a web profile".into());
    }
    let evidence = nxrs_build::deployment::validate(root, &app.manifest, &profile, os)?;
    let out = out.unwrap_or_else(|| {
        root.join("target/build")
            .join(&app.bin)
            .join(profile.name())
    });
    if dry {
        println!("{}",serde_json::to_string_pretty(&serde_json::json!({"deployment":evidence,"output":out,"backend":os,"bin":app.bin,"command":app.command})).unwrap());
        return Ok(ExitCode::SUCCESS);
    }
    match os {
        "nuttx" => nxrs_build::nuttx::build(root, &app, &profile, &out, &options)?,
        "emscripten" => nxrs_build::web::build(
            root,
            &app,
            &profile,
            &out,
            &options,
            html.as_deref(),
            web_fix,
        )?,
        _ => build::host(root, &app, &profile, &out, &options)?,
    };
    Ok(ExitCode::SUCCESS)
}
fn profile_command(root: &Path, args: &[String]) -> Result<ExitCode> {
    let mut name = None;
    let mut os = None;
    let mut capability = None;
    let mut features = None;
    let mut helpers = false;
    let mut target = None;
    let mut options = build::Options::default();
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--platform" => name = Some(args.next().ok_or("missing --platform value")?),
            "--target-os" => os = Some(args.next().ok_or("missing --target-os value")?),
            "--capability" => capability = Some(args.next().ok_or("missing --capability value")?),
            "--model-features" => features = Some(args.next().ok_or("missing manifest path")?),
            "--native-helpers" => helpers = true,
            "--cargo-target" => target = Some(args.next().ok_or("missing --cargo-target")?),
            "--features" | "--feature" => {
                let value = args.next().ok_or("missing Cargo feature list")?;
                options.features.extend(value.split(|c: char| c == ',' || c.is_whitespace())
                    .filter(|s| !s.is_empty()).map(str::to_owned));
            }
            "--no-default-features" => options.no_default_features = true,
            _ => return Err(format!("unknown profile argument {arg}")),
        }
    }
    let profile = Profile::resolve(root, name.ok_or("profile requires --platform")?)?;
    if let Some(os) = os {
        profile.validate_target(os)?;
    }
    if let Some(capability) = capability {
        for line in profile.cargo_output(
            root, capability, os.ok_or("--capability requires --target-os")?
        )? {
            println!("{line}");
        }
    } else if let Some(manifest) = features {
        options.validate()?;
        let target = match target {
            Some(target) => target.to_string(),
            None if profile.data.get("board").is_some() => {
                return Err("NuttX model selection requires --cargo-target <qualified-target.json>".into());
            }
            None => nxrs_build::selection::host_target(&mut std::process::Command::new("rustc"))?,
        };
        let selected = nxrs_build::selection::CargoSelection::resolve(
            &root.join(manifest), &options, Path::new(&target), std::process::Command::new("cargo"),
        )?;
        println!("{}", serde_json::to_string(&profile.model_features(&selected)?).unwrap());
    } else if helpers {
        println!("{}", serde_json::to_string(&profile.native_helpers(root)?).unwrap());
    } else {
        println!("{}", serde_json::to_string_pretty(&profile.json()).unwrap());
    }
    Ok(ExitCode::SUCCESS)
}

fn run() -> Result<ExitCode> {
    let mut raw = env::args_os()
        .skip(1)
        .map(|arg| {
            arg.into_string()
                .map_err(|_| "arguments must be valid UTF-8".to_owned())
        })
        .collect::<Result<Vec<_>>>()?;
    if env::current_exe()
        .map_err(|e| e.to_string())?
        .file_stem()
        .is_some_and(|s| s == "nxrs-nuttx-link")
    {
        nxrs_build::linker::link(&raw)?;
        return Ok(ExitCode::SUCCESS);
    }
    if raw.first().is_some_and(|s| s == "nuttx-link") {
        nxrs_build::linker::link(&raw[1..])?;
        return Ok(ExitCode::SUCCESS);
    }
    let requested_root = if raw.first().is_some_and(|s| s == "--workspace-root") {
        let path = raw.get(1).ok_or("missing workspace root")?;
        let root = PathBuf::from(path);
        raw.drain(..2);
        Some(root)
    } else {
        None
    };
    let command = raw.first().map(String::as_str).unwrap_or("--help");
    let help = match command {
        "-h" | "--help" => Some(usage()),
        "build" | "nuttx-build" if raw[1..].iter().any(|s| s == "-h" || s == "--help") => {
            Some(build_usage())
        }
        "profile" if raw[1..].iter().any(|s| s == "-h" || s == "--help") => Some(profile_usage()),
        _ => None,
    };
    if let Some(help) = help {
        println!("{help}");
        return Ok(ExitCode::SUCCESS);
    }
    let root = match requested_root {
        Some(path) => path.canonicalize().map_err(|e| e.to_string())?,
        None => workspace_root()?,
    };
    match command {
        "platforms" | "--list-platforms" => {
            for target in Target::ALL {
                println!("{}", target.name());
            }
        }
        "apps" | "--list-apps" => applications(&root)?,
        "inspect-profile" => {
            let request =
                nxrs_build::json::from_reader(std::io::stdin()).map_err(|e| e.to_string())?;
            println!("{}", nxrs_build::inspect::query(request)?);
        }
        "profile" => return profile_command(&root, &raw[1..]),
        "build" => return build_command(&root, &raw[1..]),
        "nuttx-build" => {
            let args: Vec<_> = raw[1..]
                .iter()
                .map(|s| {
                    if s == "--platform" {
                        "--profile".into()
                    } else {
                        s.clone()
                    }
                })
                .collect();
            return build_command(&root, &args);
        }
        "check"
        | "prepare-std"
        | "patch-source"
        | "stamp-version"
        | "sign"
        | "prepare-radio"
        | "hal-native"
        | "copy-hal-cache"
        | "build-loader"
        | "detector-sdk"
        | "patch-file"
        | "factory"
        | "prepare-browser-std" => nxrs_build::commands::run(&root, command, &raw[1..])?,
        "fetch" if raw.get(1).map(String::as_str) == Some("detector") => {
            let mut destination = root.join("target/pico-sdk");
            let mut checkout = None;
            let mut args = raw[2..].iter();
            while let Some(arg) = args.next() {
                match arg.as_str() {
                    "--out" => {
                        destination = PathBuf::from(args.next().ok_or("missing SDK destination")?)
                    }
                    "--checkout" => {
                        checkout = Some(PathBuf::from(args.next().ok_or("missing SDK checkout")?))
                    }
                    _ => return Err(format!("unknown fetch argument {arg}")),
                }
            }
            nxrs_build::source::prepare_inventory(
                &root.join("upstream/esp-dl/source.json"),
                &root.join("upstream/esp-dl/port.patch"),
                &destination,
                checkout.as_deref(),
            )?;
        }
        "fetch" if raw.get(1).map(String::as_str) == Some("tls") => {
            let pin = nxrs_build::read_json(&root.join("upstream/mbedtls/source.json"))?;
            let default = root.join("target").join(format!(
                "mbedtls-{}.tar.bz2",
                pin["version"].as_str().ok_or("invalid TLS version")?
            ));
            let path = match &raw[2..] {
                [] => default,
                [flag, path] if flag == "--out" => PathBuf::from(path),
                _ => return Err("usage: cargo nxrs fetch tls [--out path]".into()),
            };
            nxrs_build::fetch_verified(
                pin["url"].as_str().ok_or("invalid source URL")?,
                pin["sha256"].as_str().ok_or("invalid source hash")?,
                pin["size"].as_u64().ok_or("invalid source size")?,
                &path,
            )?;
            println!("{}", path.display());
        }
        _ if command.starts_with("--") => return build_command(&root, &raw),
        _ => return Err(format!("unknown command {command}\n{}", usage())),
    }
    Ok(ExitCode::SUCCESS)
}
fn main() -> ExitCode {
    match run() {
        Ok(code) => code,
        Err(e) => {
            eprintln!("cargo nxrs: {e}");
            ExitCode::FAILURE
        }
    }
}
