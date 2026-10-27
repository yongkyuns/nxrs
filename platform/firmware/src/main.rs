//! Cargo-facing firmware build frontend.
//!
//! Developer-facing selection is app + platform. The NuttX/Kconfig/link backend
//! remains a focused implementation detail under tools/.
#![forbid(unsafe_code)]

use std::env;
use std::ffi::OsString;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

#[derive(Debug)]
struct AppConfig {
    manifest: PathBuf,
    package: String,
    bin: String,
    command: String,
    priority: u32,
    stack_size: u32,
}

#[derive(Debug, Default)]
struct Args {
    app: Option<String>,
    platform: Option<String>,
    out: Option<PathBuf>,
    list_apps: bool,
    list_platforms: bool,
    dry_run: bool,
    boot_plan: bool,
    bootloader: bool,
    boot_policy: Option<PathBuf>,
    boot_policy_sha256: Option<String>,
    help: bool,
}

fn usage() -> &'static str {
    "usage:
  cargo firmware --app <app> --platform <platform> [--out <path>] [--dry-run] [--boot-policy <file> --boot-policy-sha256 <sha>]
  cargo firmware --platform <platform> --emit-boot-plan [--out <path>]
  cargo firmware --platform <platform> --bootloader --boot-policy <file> --boot-policy-sha256 <sha> [--out <path>]
  cargo firmware --list-apps
  cargo firmware --list-platforms"
}

fn parse_args() -> Result<Args, String> {
    let mut parsed = Args::default();
    let mut args = env::args_os().skip(1);
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--app") => {
                parsed.app = Some(next_utf8(&mut args, "--app")?);
            }
            Some("--platform") => {
                parsed.platform = Some(next_utf8(&mut args, "--platform")?);
            }
            Some("--out") => {
                parsed.out = Some(PathBuf::from(next_utf8(&mut args, "--out")?));
            }
            Some("--emit-boot-plan") => parsed.boot_plan = true,
            Some("--bootloader") => parsed.bootloader = true,
            Some("--boot-policy") => {
                parsed.boot_policy = Some(PathBuf::from(next_utf8(&mut args, "--boot-policy")?))
            }
            Some("--boot-policy-sha256") => {
                parsed.boot_policy_sha256 = Some(next_utf8(&mut args, "--boot-policy-sha256")?)
            }
            Some("--list-apps") => parsed.list_apps = true,
            Some("--list-platforms") => parsed.list_platforms = true,
            Some("--dry-run") => parsed.dry_run = true,
            Some("-h" | "--help") => parsed.help = true,
            Some(other) => {
                return Err(format!(
                    "unknown argument: {other}
{}",
                    usage()
                ))
            }
            None => {
                return Err(format!(
                    "arguments must be valid UTF-8
{}",
                    usage()
                ))
            }
        }
    }
    Ok(parsed)
}

fn next_utf8(args: &mut impl Iterator<Item = OsString>, flag: &str) -> Result<String, String> {
    args.next()
        .ok_or_else(|| format!("missing value for {flag}"))
        .and_then(|value| {
            value
                .into_string()
                .map_err(|_| format!("value for {flag} must be valid UTF-8"))
        })
}

fn valid_name(value: &str) -> bool {
    let mut chars = value.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

fn workspace_root() -> io::Result<PathBuf> {
    let mut directory = env::current_dir()?;
    loop {
        let manifest = directory.join("Cargo.toml");
        if manifest.is_file() {
            let text = fs::read_to_string(&manifest)?;
            if text.lines().any(|line| line.trim() == "[workspace]") {
                return Ok(directory);
            }
        }
        if !directory.pop() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "could not locate Nxrs workspace root",
            ));
        }
    }
}

fn unquote(value: &str) -> Option<String> {
    let value = value.trim();
    value
        .strip_prefix('"')
        .and_then(|value| value.strip_suffix('"'))
        .map(ToOwned::to_owned)
}

fn key_value(line: &str) -> Option<(&str, &str)> {
    let line = line.split('#').next()?.trim();
    if line.is_empty() {
        return None;
    }
    let (key, value) = line.split_once('=')?;
    Some((key.trim(), value.trim()))
}

fn load_app(root: &Path, app: &str) -> Result<AppConfig, String> {
    if !valid_name(app) {
        return Err(format!("invalid app name: {app}"));
    }
    let manifest = root.join("app").join(app).join("Cargo.toml");
    let text = fs::read_to_string(&manifest)
        .map_err(|error| format!("cannot read {}: {error}", manifest.display()))?;

    let mut section = "";
    let mut package = None;
    let mut bin = None;
    let mut command = None;
    let mut priority = None;
    let mut stack_size = None;

    for raw in text.lines() {
        let line = raw.trim();
        if line.starts_with('[') && line.ends_with(']') {
            section = line;
            continue;
        }
        let Some((key, value)) = key_value(line) else {
            continue;
        };
        match section {
            "[package]" if key == "name" => package = unquote(value),
            "[package.metadata.nxrs.firmware]" => match key {
                "bin" => bin = unquote(value),
                "command" => command = unquote(value),
                "priority" => priority = value.parse::<u32>().ok(),
                "stack-size" => stack_size = value.parse::<u32>().ok(),
                _ => {}
            },
            _ => {}
        }
    }

    let manifest_display = manifest.display().to_string();
    let missing =
        |name: &str| format!("{manifest_display} is missing package.metadata.nxrs.firmware.{name}");
    let package = package.ok_or_else(|| format!("{manifest_display} is missing package.name"))?;

    Ok(AppConfig {
        manifest,
        package,
        bin: bin.ok_or_else(|| missing("bin"))?,
        command: command.ok_or_else(|| missing("command"))?,
        priority: priority.ok_or_else(|| missing("priority"))?,
        stack_size: stack_size.ok_or_else(|| missing("stack-size"))?,
    })
}

fn list_apps(root: &Path) -> Result<(), String> {
    let app_root = root.join("app");
    let entries = fs::read_dir(&app_root)
        .map_err(|error| format!("cannot read {}: {error}", app_root.display()))?;
    let mut names = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|error| error.to_string())?;
        if !entry
            .file_type()
            .map_err(|error| error.to_string())?
            .is_dir()
        {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        if load_app(root, &name).is_ok() {
            names.push(name);
        }
    }
    names.sort();
    for name in names {
        println!("{name}");
    }
    Ok(())
}

fn list_platforms(root: &Path) -> Result<(), String> {
    let platform_root = root.join("platform/nuttx/platforms");
    let entries = fs::read_dir(&platform_root)
        .map_err(|error| format!("cannot read {}: {error}", platform_root.display()))?;
    let mut names = entries
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_file()))
        .filter_map(|entry| {
            let path = entry.path();
            (path.extension().and_then(|ext| ext.to_str()) == Some("toml"))
                .then(|| path.file_stem()?.to_str().map(ToOwned::to_owned))
                .flatten()
        })
        .collect::<Vec<_>>();
    names.sort();
    for name in names {
        println!("{name}");
    }
    Ok(())
}

fn display_command(command: &Command) -> String {
    let mut parts = vec![command.get_program().to_string_lossy().into_owned()];
    parts.extend(
        command
            .get_args()
            .map(|arg| format!("{:?}", arg.to_string_lossy())),
    );
    parts.join(" ")
}

fn run() -> Result<ExitCode, String> {
    let args = parse_args()?;
    if args.help {
        println!("{}", usage());
        return Ok(ExitCode::SUCCESS);
    }

    let root = workspace_root().map_err(|error| error.to_string())?;

    if args.list_apps || args.list_platforms {
        if args.app.is_some()
            || args.platform.is_some()
            || args.out.is_some()
            || args.boot_plan
            || args.bootloader
            || args.boot_policy.is_some()
            || args.boot_policy_sha256.is_some()
        {
            return Err(format!(
                "list commands cannot be combined with build arguments
{}",
                usage()
            ));
        }
        if args.list_apps {
            list_apps(&root)?;
        }
        if args.list_platforms {
            list_platforms(&root)?;
        }
        return Ok(ExitCode::SUCCESS);
    }

    if args.boot_plan && args.bootloader {
        return Err("select one boot build mode".into());
    }
    if args.boot_policy.is_some() != args.boot_policy_sha256.is_some() {
        return Err("--boot-policy and --boot-policy-sha256 are required together".into());
    }
    let app_name = if args.boot_plan || args.bootloader {
        if args.app.is_some() {
            return Err("boot plan/bootloader modes do not select an application".into());
        }
        ""
    } else {
        args.app
            .as_deref()
            .ok_or_else(|| format!("missing --app\n{}", usage()))?
    };
    let platform = args.platform.as_deref().ok_or_else(|| {
        format!(
            "missing --platform
{}",
            usage()
        )
    })?;
    if !valid_name(platform) {
        return Err(format!("invalid platform name: {platform}"));
    }

    let platform_profile = root
        .join("platform/nuttx/platforms")
        .join(format!("{platform}.toml"));
    if !platform_profile.is_file() {
        return Err(format!(
            "unknown platform {platform}: {} does not exist",
            platform_profile.display()
        ));
    }

    if args.boot_plan || args.bootloader {
        if args.boot_plan && args.boot_policy.is_some() {
            return Err("boot plans are independent of signing keys".into());
        }
        if args.bootloader && args.boot_policy.is_none() {
            return Err(
                "bootloader builds require the public policy and its independent SHA-256 pin"
                    .into(),
            );
        }
        let mode = if args.boot_plan { "plan" } else { "build" };
        let out = args
            .out
            .unwrap_or_else(|| root.join("target/firmware").join(mode).join(platform));
        let python = env::var_os("NXRS_BOOT_PYTHON").unwrap_or_else(|| OsString::from("python3"));
        let mut command = Command::new(python);
        command
            .current_dir(&root)
            .arg(root.join("tools/boot/build_esp32s3.py"))
            .arg(mode)
            .arg("--platform")
            .arg(platform)
            .arg("--out")
            .arg(&out);
        if let (Some(policy), Some(pin)) = (&args.boot_policy, &args.boot_policy_sha256) {
            command
                .arg("--policy")
                .arg(policy)
                .arg("--policy-sha256")
                .arg(pin);
        }
        if args.dry_run {
            println!("{}", display_command(&command));
            return Ok(ExitCode::SUCCESS);
        }
        let status = command
            .status()
            .map_err(|error| format!("failed to execute boot build backend: {error}"))?;
        return Ok(ExitCode::from(status.code().unwrap_or(1) as u8));
    }
    let app = load_app(&root, app_name)?;
    let out = args.out.unwrap_or_else(|| {
        root.join("target")
            .join("firmware")
            .join(app_name)
            .join(platform)
    });

    let mut command = Command::new("bash");
    command
        .current_dir(&root)
        .arg(root.join("tools/build-nuttx-std-app.sh"))
        .arg("--app-manifest")
        .arg(&app.manifest)
        .arg("--app-package")
        .arg(&app.package)
        .arg("--bin")
        .arg(&app.bin)
        .arg("--command")
        .arg(&app.command)
        .arg("--priority")
        .arg(app.priority.to_string())
        .arg("--stack-size")
        .arg(app.stack_size.to_string())
        .arg("--platform")
        .arg(platform)
        .arg("--out")
        .arg(&out);

    if let (Some(policy), Some(pin)) = (&args.boot_policy, &args.boot_policy_sha256) {
        command
            .arg("--boot-policy")
            .arg(policy)
            .arg("--boot-policy-sha256")
            .arg(pin);
    }
    if args.dry_run {
        println!("{}", display_command(&command));
        return Ok(ExitCode::SUCCESS);
    }

    println!(
        "Building app={} platform={} -> {}",
        app_name,
        platform,
        out.display()
    );
    let status = command
        .status()
        .map_err(|error| format!("failed to execute firmware backend: {error}"))?;
    Ok(ExitCode::from(status.code().unwrap_or(1) as u8))
}

fn main() -> ExitCode {
    match run() {
        Ok(code) => code,
        Err(error) => {
            eprintln!("cargo firmware: {error}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::valid_name;

    #[test]
    fn names_are_path_safe() {
        for name in ["event-demo", "pico2-mock", "v1.2", "a_b"] {
            assert!(valid_name(name), "{name}");
        }
        for name in ["", "../pico2", "/tmp/x", "a/b", "-leading"] {
            assert!(!valid_name(name), "{name}");
        }
    }
}
