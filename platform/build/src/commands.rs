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

fn native_app(out: &Path, app: &Path) -> Result<PathBuf> {
    let out = out.canonicalize().map_err(|e| e.to_string())?;
    let app = app.canonicalize().map_err(|e| e.to_string())?;
    if !app.starts_with(out.join("apps")) || !app.join("Makefile").is_file() {
        return Err(
            "native SDK app must be an existing Makefile directory inside output/apps".into(),
        );
    }
    Ok(app)
}

/// Legacy qualification builds use the same ledger checks and helper staging as
/// the ordinary NuttX frontend, then let the target Makefile compile the sources.
fn stage_std_native(sysroot: &Path, receipt: &Path, app: &Path) -> Result<()> {
    let sources = sdk::native_sources(sysroot, receipt)?;
    let (_, names) = crate::nuttx::stage_helpers(&sources, app)?;
    crate::reuse::write_if_changed(
        &app.join("std-native.mk"),
        format!("CSRCS += {}\n", names.join(" ")).as_bytes(),
    )
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
            let p = Profile::resolve(root, a.get("--profile")?)?;
            let evidence =
                deployment::validate(root, &a.path("--manifest-path")?, &p, a.get("--target-os")?)?;
            if let Some(path) = a.values.get("--out") {
                write_json(Path::new(path), &evidence)?;
            }
            println!("{}", crate::json::to_string_pretty(&evidence).unwrap());
        }
        "prepare-std" => {
            let a = Args::parse(
                args,
                &["--source", "--output", "--sdk", "--native-app"],
                &[],
            )?;
            let out = a.path("--output")?;
            let app = a
                .values
                .get("--native-app")
                .map(|path| native_app(&out, Path::new(path)))
                .transpose()?;
            let sysroot = sdk::prepare(root, &a.path("--source")?, &out, a.get("--sdk")?)?;
            if let Some(app) = app {
                stage_std_native(&sysroot, &out.join("std-patch.json"), &app)?;
            }
            println!("{}", sysroot.display());
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn native_app_must_stay_in_the_owned_apps_tree() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let out = temp.0.join("output");
        let app = out.join("apps/examples/probe");
        fs::create_dir_all(&app).unwrap();
        fs::write(app.join("Makefile"), "# fixture").unwrap();
        assert_eq!(native_app(&out, &app).unwrap(), app.canonicalize().unwrap());

        let outside = temp.0.join("outside");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("Makefile"), "# untouched").unwrap();
        let args = [
            "--source".into(),
            temp.0.join("missing-sdk").display().to_string(),
            "--output".into(),
            out.display().to_string(),
            "--sdk".into(),
            "nightly-2025-09-15".into(),
            "--native-app".into(),
            outside.display().to_string(),
        ];
        assert!(run(&temp.0, "prepare-std", &args)
            .unwrap_err()
            .contains("inside output/apps"));
        assert!(!out.join("toolchain").exists());
        assert_eq!(
            fs::read_to_string(outside.join("Makefile")).unwrap(),
            "# untouched"
        );
        #[cfg(unix)]
        {
            let link = out.join("apps/escaped");
            std::os::unix::fs::symlink(&outside, &link).unwrap();
            assert!(native_app(&out, &link).is_err());
        }
    }

    #[cfg(unix)]
    #[test]
    fn legacy_std_makefiles_link_the_verified_sdk_binding() {
        use std::process::Command;

        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let out = temp.0.join("output");
        let sysroot = out.join("toolchain");
        fs::create_dir_all(&sysroot).unwrap();
        let binding = "lib/rustlib/src/rust/library/std/src/sys/sync/mpmc/nuttx/ffi/lock.c";
        // Compile the actual patched binding; the header below substitutes only
        // the target operations, so this proves build wiring rather than IRQ ABI.
        crate::run(
            Command::new("git")
                .current_dir(&sysroot)
                .args(["apply", &format!("--include={binding}")])
                .arg(root.join("upstream/rust-std/patches/0007-nuttx-bounded-channel.patch")),
        )
        .unwrap();
        let source = sysroot.join(binding);
        let receipt = out.join("std-patch.json");
        let apps = out.join("apps");
        fs::create_dir_all(apps.join("include/nuttx")).unwrap();
        fs::write(apps.join("Make.defs"), "").unwrap();
        fs::write(
            apps.join("include/nuttx/spinlock.h"),
            "typedef uintptr_t spinlock_t;\ntypedef uintptr_t irqstate_t;\n\
             static inline void spin_lock_init(spinlock_t *p) { *p = 0; }\n\
             static inline irqstate_t spin_lock_irqsave(volatile spinlock_t *p) { *p = 1; return 7; }\n\
             static inline void spin_unlock_irqrestore(volatile spinlock_t *p, irqstate_t f) { *p = f; }\n",
        )
        .unwrap();
        fs::write(
            apps.join("probe.c"),
            "#include <stdint.h>\n\
             void __rust_nuttx_spin_init(void *);\n\
             uintptr_t __rust_nuttx_spin_lock(void *);\n\
             void __rust_nuttx_spin_unlock(void *, uintptr_t);\n\
             int main(void) { uintptr_t storage[2]; __rust_nuttx_spin_init(storage);\n\
               uintptr_t flags = __rust_nuttx_spin_lock(storage);\n\
               __rust_nuttx_spin_unlock(storage, flags); return storage[0] != 7; }\n",
        )
        .unwrap();
        fs::write(
            apps.join("Application.mk"),
            ".PHONY: sdk-native-probe\nsdk-native-probe:\n\
             \t$(CC) -std=c11 -Wall -Werror -I$(APPDIR)/include $(APPDIR)/probe.c \
               $(filter target_helper_%.c,$(CSRCS)) -o $@\n",
        )
        .unwrap();
        for (index, template) in [
            "tests/nuttx-std/nuttx/Makefile",
            "tests/nuttx-std/resources/nuttx/Makefile",
            "platform/nuttx/qualification/Makefile",
        ]
        .iter()
        .enumerate()
        {
            let app = apps.join(format!("examples/probe{index}"));
            fs::create_dir_all(&app).unwrap();
            fs::copy(root.join(template), app.join("Makefile")).unwrap();
            let app = native_app(&out, &app).unwrap();
            let mut make = Command::new("make");
            make.current_dir(&app).args([
                "--no-print-directory",
                &format!("APPDIR={}", apps.display()),
                "NXRS_STD_ELF=unused.o",
                "sdk-native-probe",
            ]);
            let missing = make.output().unwrap();
            assert!(
                !missing.status.success(),
                "{template} omitted the missing-symbol witness"
            );
            assert!(String::from_utf8_lossy(&missing.stderr).contains("__rust_nuttx_spin_"));

            write_json(&receipt, &crate::json::json!({})).unwrap();
            assert!(stage_std_native(&sysroot, &receipt, &app).is_err());
            assert!(!app.join("std-native.mk").exists());
            write_json(
                &receipt,
                &crate::json::json!({"native_sources":[{"path":binding,"sha256":crate::hash(&source).unwrap()}]}),
            )
            .unwrap();
            stage_std_native(&sysroot, &receipt, &app).unwrap();
            assert_eq!(
                fs::read(app.join("target_helper_0.c")).unwrap(),
                fs::read(&source).unwrap()
            );
            crate::run(&mut make).unwrap();
            crate::run(&mut Command::new(app.join("sdk-native-probe"))).unwrap();
        }
    }
}
