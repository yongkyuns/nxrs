//! Small CLI adapters for source preparation and qualification tools.
use crate::{boot, deployment, native, profile::Profile, radio, sdk, source, write_json, Result};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};
struct Args {
    values: BTreeMap<String, String>,
    flags: BTreeSet<String>,
}
impl Args {
    fn parse(args: &[String], values: &[&str], flags: &[&str]) -> Result<Self> {
        let mut result = Self {
            values: BTreeMap::new(),
            flags: BTreeSet::new(),
        };
        let mut args = args.iter();
        while let Some(arg) = args.next() {
            if values.contains(&arg.as_str()) {
                let value = args
                    .next()
                    .ok_or_else(|| format!("missing value for {arg}"))?;
                if result.values.insert(arg.clone(), value.clone()).is_some() {
                    return Err(format!("duplicate {arg}"));
                }
            } else if flags.contains(&arg.as_str()) {
                if !result.flags.insert(arg.clone()) {
                    return Err(format!("duplicate {arg}"));
                }
            } else {
                return Err(format!("unknown argument {arg}"));
            }
        }
        Ok(result)
    }
    fn get(&self, key: &str) -> Result<&str> {
        self.values
            .get(key)
            .map(String::as_str)
            .ok_or_else(|| format!("required {key}"))
    }
    fn path(&self, key: &str) -> Result<PathBuf> {
        std::path::absolute(self.get(key)?).map_err(|e| e.to_string())
    }
}
pub fn run(root: &Path, command: &str, args: &[String]) -> Result<()> {
    match command {
        "prepare-browser-std" => {
            let a = Args::parse(args, &["--source", "--output"], &[])?;
            crate::web::prepare_std(&a.path("--source")?, &a.path("--output")?)?;
        }
        "factory" => {
            let a = Args::parse(
                args,
                &[
                    "--profile",
                    "--boot-build",
                    "--application",
                    "--rom-root",
                    "--app-root",
                    "--flash-key",
                    "--output",
                ],
                &[],
            )?;
            let p = Profile::resolve(root, a.get("--profile")?)?;
            crate::factory::prepare(
                &p,
                crate::factory::Inputs {
                    boot: &a.path("--boot-build")?,
                    application: &a.path("--application")?,
                    rom_root: &a.path("--rom-root")?,
                    app_root: &a.path("--app-root")?,
                    flash_key: &a.path("--flash-key")?,
                },
                &a.path("--output")?,
            )?;
        }
        "detector-sdk" => {
            let a = Args::parse(args, &["--sdk", "--out", "--target"], &[])?;
            crate::detector_sdk::build(
                root,
                &a.path("--sdk")?,
                &a.path("--out")?,
                a.get("--target")?,
            )?;
        }
        "patch-file" => {
            let a = Args::parse(args, &["--source", "--patch", "--out", "--name"], &[])?;
            crate::sdk::apply_dependency_patch(
                &a.path("--source")?,
                &a.path("--patch")?,
                &a.path("--out")?,
                a.get("--name")?,
            )?;
        }
        "build-loader" => {
            let a = Args::parse(
                args,
                &[
                    "--profile",
                    "--mcuboot-repo",
                    "--hal-repo",
                    "--root",
                    "--out",
                    "--rom-key",
                    "--rom-root",
                ],
                &[],
            )?;
            let p = Profile::resolve(root, a.get("--profile")?)?;
            let rom = match (a.values.get("--rom-key"), a.values.get("--rom-root")) {
                (None, None) => None,
                (Some(key), Some(root)) => Some((Path::new(key), Path::new(root))),
                _ => return Err("both ROM key and independent root are required".into()),
            };
            boot::build_loader(
                root,
                &p,
                &a.path("--mcuboot-repo")?,
                &a.path("--hal-repo")?,
                &a.path("--root")?,
                &a.path("--out")?,
                rom,
            )?;
        }
        "check" => {
            let a = Args::parse(
                args,
                &["--manifest-path", "--profile", "--target-os", "--out"],
                &[],
            )?;
            let selected_root = deployment::workspace_root(&a.path("--manifest-path")?)?;
            let p = Profile::resolve(&selected_root, a.get("--profile")?)?;
            let evidence = deployment::validate(
                &selected_root,
                &a.path("--manifest-path")?,
                &p,
                a.get("--target-os")?,
            )?;
            if let Some(path) = a.values.get("--out") {
                write_json(Path::new(path), &evidence)?;
            }
            println!("{}", crate::json::to_string_pretty(&evidence).unwrap());
        }
        "prepare-std" => {
            let a = Args::parse(args, &["--source", "--output", "--sdk"], &[])?;
            println!(
                "{}",
                sdk::prepare(
                    root,
                    &a.path("--source")?,
                    &a.path("--output")?,
                    a.get("--sdk")?
                )?
                .display()
            );
        }
        "patch-source" => {
            let a = Args::parse(
                args,
                &["--source", "--revision", "--record", "--component"],
                &["--build-checkout"],
            )?;
            source::apply_series(
                root,
                &a.path("--source")?,
                a.get("--component")?,
                a.get("--revision")?,
                &a.path("--record")?,
                a.flags.contains("--build-checkout"),
            )?;
        }
        "stamp-version" => {
            let a = Args::parse(args, &["--source", "--revision"], &[])?;
            source::stamp(root, &a.path("--source")?, a.get("--revision")?)?;
        }
        "sign" => {
            let a = Args::parse(
                args,
                &[
                    "--profile",
                    "--input",
                    "--output",
                    "--key",
                    "--root",
                    "--version",
                    "--security-counter",
                ],
                &["--confirm"],
            )?;
            let p = Profile::resolve(root, a.get("--profile")?)?;
            boot::image_class(&p)?;
            let signing = boot::Signing {
                key: a.path("--key")?,
                root: a.path("--root")?,
                version: a.get("--version")?.into(),
                counter: a
                    .get("--security-counter")?
                    .parse()
                    .map_err(|_| "invalid security counter")?,
            };
            signing.sign(
                &p,
                &a.path("--input")?,
                &a.path("--output")?,
                a.flags.contains("--confirm"),
            )?;
        }
        "prepare-radio" => {
            let a = Args::parse(args, &["--out", "--record", "--release"], &["--bluetooth"])?;
            let release = a
                .values
                .get("--release")
                .cloned()
                .or_else(|| std::env::var("NXRS_PICO_RADIO_RELEASE").ok())
                .unwrap_or("pico-sdk-2.3.1".into());
            let cache = match a.values.get("--out") {
                Some(p) => PathBuf::from(p),
                None => root.join("target/radio-deps/cyw43439"),
            };
            let record = a.values.get("--record").map(PathBuf::from);
            let (binary, _, _) = radio::prepare_cached(
                &cache,
                record.as_deref(),
                if a.flags.contains("--bluetooth") {
                    "cyw43439-bluetooth"
                } else {
                    "cyw43439"
                },
                &release,
            )?;
            println!("{}", binary.display());
        }
        "hal-native" => {
            let a = Args::parse(
                args,
                &["--capability-dir", "--os", "--target", "--out"],
                &[],
            )?;
            for line in native::build(
                &a.path("--capability-dir")?,
                a.get("--os")?,
                a.get("--target")?,
                &a.path("--out")?,
            )? {
                println!("{line}");
            }
        }
        "copy-hal-cache" => {
            let a = Args::parse(args, &["--cache", "--destination", "--revision"], &[])?;
            crate::nuttx::copy_hal_cache(
                &a.path("--cache")?,
                &a.path("--destination")?,
                a.get("--revision")?,
            )?;
        }
        _ => return Err(format!("unknown build utility {command}")),
    }
    Ok(())
}
