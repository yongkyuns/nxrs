//! Real Cargo/CC witness for private helper ownership and incremental headers.
//! The host C ABI is intentional here; firmware ABI remains a target-build gate.
#![cfg(unix)]
use nxrs_build::{find_program, hash, json::json, read_json, shell_quote, write_json, TempDir};
use nxrs_build_support::{prepared_resource, profile::Profile};
use std::{
    collections::BTreeSet, env, fs, os::unix::fs::PermissionsExt, path::Path, process::Command,
};

const TARGET: &str = "xtensa-esp32s3-nuttx";
const SDK: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../..");

fn build(root: &Path, extra: &[&str]) -> std::process::Output {
    let mut command = Command::new(env!("CARGO"));
    command
        .current_dir(root)
        .args([
            "build",
            "--offline",
            "--locked",
            "-p",
            "consumer",
            "--message-format=json",
        ])
        .args(extra)
        .env("CARGO_TARGET_DIR", root.join("target"))
        .env("NXRS_NUTTX_NATIVE_CONFIG", root.join("native.json"))
        .env("NXRS_PLATFORM", "m5stack-cores3-gnss")
        .env("NXRS_SDK_ROOT", SDK)
        .env_remove("NXRS_RESOLVED_PROFILE")
        .env_remove("CARGO_BUILD_TARGET")
        .env_remove("RUSTFLAGS")
        .env_remove("CARGO_ENCODED_RUSTFLAGS")
        .env_remove(prepared_resource::ENVIRONMENT);
    let cache = root.join("prepared");
    if cache.is_dir() {
        command.env(prepared_resource::ENVIRONMENT, cache);
    }
    command.output().unwrap()
}

fn runs(root: &Path, output: &std::process::Output) -> usize {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let messages: Vec<serde_json::Value> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let script = messages
        .iter()
        .find(|m| {
            m["reason"] == "build-script-executed"
                && m["package_id"].as_str().unwrap().contains("nxrs-gnss")
        })
        .unwrap();
    assert!(script["linked_libs"]
        .as_array()
        .unwrap()
        .contains(&json!("static=gnss_native")));
    assert!(!messages
        .iter()
        .any(|m| m["reason"] == "build-script-executed"
            && m["package_id"].as_str().unwrap().contains("unused-helper")));
    let binary = root.join("target/debug/consumer");
    assert!(Command::new(binary).status().unwrap().success());
    fs::read_to_string(Path::new(script["out_dir"].as_str().unwrap()).join("runs"))
        .unwrap()
        .lines()
        .count()
}

#[test]
fn only_consumed_helpers_compile_and_cargo_revisits_changed_native_inputs() {
    let temp = TempDir::new(&std::env::temp_dir()).unwrap();
    let root = &temp.0;
    let support = Path::new(env!("CARGO_MANIFEST_DIR")).join("../build-support");
    fs::write(
        root.join("Cargo.toml"),
        "[workspace]\nmembers=[\"consumer\",\"selected\",\"unused\"]\nresolver=\"2\"\n",
    )
    .unwrap();
    for (directory, package) in [("selected", "nxrs-gnss"), ("unused", "unused-helper")] {
        let path = root.join(directory);
        fs::create_dir_all(path.join("src/platform/nuttx/ffi")).unwrap();
        let metadata = if directory == "selected" {
            "[features]\nhelper=[]\n[package.metadata.nxrs]\nkind=\"hal-capability\"\n[package.metadata.nxrs.platform.nuttx]\nrequired-features=[\"helper\"]\n"
        } else {
            ""
        };
        fs::write(path.join("Cargo.toml"),format!("[package]\nname={package:?}\nversion=\"0.0.0\"\nedition=\"2021\"\n{metadata}[build-dependencies]\nnxrs-build-support={{path={support:?}}}\n")).unwrap();
        fs::write(path.join("src/lib.rs"),"#[cfg(feature=\"helper\")] extern \"C\" { fn device_value() -> i32; }\npub fn value() -> i32 { #[cfg(feature=\"helper\")] { unsafe { device_value() } } #[cfg(not(feature=\"helper\"))] { 1 } }\n").unwrap();
        fs::write(path.join("build.rs"),r#"
use std::{env,io::Write,path::PathBuf};
fn main() {
    let out=PathBuf::from(env::var_os("OUT_DIR").unwrap());
    env::set_var("CARGO_CFG_TARGET_OS", "nuttx");
    env::set_var("TARGET", "xtensa-esp32s3-nuttx");
    if env::var("CARGO_PKG_NAME").unwrap() == "nxrs-gnss" {
        nxrs_build_support::configure_hal().unwrap();
    } else {
        nxrs_build_support::configure_resource().unwrap();
    }
    writeln!(std::fs::OpenOptions::new().create(true).append(true).open(out.join("runs")).unwrap(),"run").unwrap();
}
"#).unwrap();
        fs::write(
            path.join("src/platform/nuttx/ffi/value.c"),
            if directory == "selected" {
                "#include <nuttx/config.h>\n#include <context.h>\nint device_value(void) { return DEVICE_ABI + CONTEXT_ABI; }\n"
            } else {
                "#error unconsumed private helper must not compile\n"
            },
        )
        .unwrap();
    }
    fs::create_dir_all(root.join("consumer/src")).unwrap();
    fs::write(root.join("consumer/Cargo.toml"),"[package]\nname=\"consumer\"\nversion=\"0.0.0\"\nedition=\"2021\"\n[features]\ndefault=[\"helper\"]\nhelper=[\"selected-helper/helper\"]\n[dependencies]\nselected-helper={package=\"nxrs-gnss\",path=\"../selected\"}\nunused-helper={path=\"../unused\",optional=true}\n").unwrap();
    fs::write(
        root.join("consumer/src/main.rs"),
        "fn main() { let value = selected_helper::value(); if let Some(expected) = std::env::args().nth(1) { assert_eq!(value, expected.parse::<i32>().unwrap()); } else { assert!(value > 0); } }\n",
    )
    .unwrap();
    let include = root.join("include");
    fs::create_dir_all(include.join("nuttx")).unwrap();
    let header = include.join("nuttx/config.h");
    fs::write(&header, "#define DEVICE_ABI 7\n").unwrap();
    let private = root.join("private architecture #$");
    fs::create_dir(&private).unwrap();
    fs::write(private.join("context.h"), "#define CONTEXT_ABI 3\n").unwrap();
    let higher_priority = root.join("higher-priority");
    fs::create_dir(&higher_priority).unwrap();
    let compiler = root.join("compiler");
    let calls = root.join("compiler-calls");
    let mutate = root.join("mutate-header");
    let quote = |path: &Path| shell_quote(path.to_str().unwrap());
    fs::write(
        &compiler,
        format!(
            "#!/bin/sh\nprintf 'compile\\n' >> {}\n{} \"$@\" || exit $?\n\
             if [ -f {} ]; then\n\
               for arg do\n\
                 if [ \"$arg\" = -c ]; then printf '#define CONTEXT_ABI 99\\n' > {}; fi\n\
               done\n\
             fi\n",
            quote(&calls),
            quote(&find_program("cc").unwrap()),
            quote(&mutate),
            quote(&private.join("context.h"))
        ),
    )
    .unwrap();
    fs::set_permissions(&compiler, fs::Permissions::from_mode(0o755)).unwrap();
    let context = root.join("native.json");
    write_json(
        &context,
        &json!({"schema":1,"target":TARGET,"directory":root,
        "include":include,"config_sha256":hash(&header).unwrap(),"cc":[compiler],"ar":[find_program("ar").unwrap(),"rcs"],
        "cflags":["-I",include.to_str().unwrap(),"-I",higher_priority.to_str().unwrap(),"-I",private.to_str().unwrap()]}),
    )
    .unwrap();
    assert!(Command::new(env!("CARGO"))
        .current_dir(root)
        .args(["generate-lockfile", "--offline"])
        .status()
        .unwrap()
        .success());
    assert_eq!(runs(root, &build(root, &[])), 1);
    assert_eq!(runs(root, &build(root, &[])), 1);
    // NuttX Make writes and removes this temporary file after Cargo. No header
    // changed, so directory mtime churn must not rerun the owning HAL's script.
    let temporary_header = include.join("nuttx/config.h.tmp");
    fs::write(&temporary_header, "temporary generated configuration").unwrap();
    fs::remove_file(&temporary_header).unwrap();
    assert_eq!(runs(root, &build(root, &[])), 1);
    fs::write(&header, "#define DEVICE_ABI 8\n").unwrap();
    let stale = build(root, &[]);
    assert!(!stale.status.success());
    assert!(String::from_utf8_lossy(&stale.stderr).contains("configured NuttX headers changed"));
    let mut data = read_json(&context).unwrap();
    data["config_sha256"] = json!(hash(&header).unwrap());
    write_json(&context, &data).unwrap();
    assert_eq!(runs(root, &build(root, &[])), 2);
    assert_eq!(runs(root, &build(root, &[])), 2);
    fs::write(private.join("context.h"), "#define CONTEXT_ABI 4\n").unwrap();
    assert_eq!(runs(root, &build(root, &[])), 3);
    // Used headers outside include/ are tracked by compiler dependencies, too.
    fs::remove_file(private.join("context.h")).unwrap();
    assert!(!build(root, &[]).status.success());
    fs::write(private.join("context.h"), "#define CONTEXT_ABI 4\n").unwrap();
    assert_eq!(runs(root, &build(root, &[])), 4);
    fs::write(
        root.join("selected/src/platform/nuttx/ffi/value.c"),
        "#include <nuttx/config.h>\nint device_value(void) { return DEVICE_ABI + 1; }\n",
    )
    .unwrap();
    assert_eq!(runs(root, &build(root, &[])), 5);
    let owner = root.join("selected").canonicalize().unwrap();
    let cores3 = Profile::resolve(Path::new(SDK), "m5stack-cores3-gnss").unwrap();
    let wroom = Profile::resolve(Path::new(SDK), "esp32s3-camera-stream-mcuboot").unwrap();
    let enabled = BTreeSet::from(["helper".to_owned()]);
    assert!(prepared_resource::selected(&owner, &cores3, &enabled).unwrap());
    assert!(!prepared_resource::selected(&owner, &cores3, &BTreeSet::new()).unwrap());
    assert!(!prepared_resource::selected(&owner, &wroom, &enabled).unwrap());
    let cache = root.join("prepared");
    let compile_count = || fs::read_to_string(&calls).unwrap().lines().count();
    let prepare = || {
        prepared_resource::prepare(
            &owner,
            TARGET,
            &context,
            &cache,
            &env::var_os("PATH").unwrap(),
        )
    };
    let before = compile_count();
    let archive = prepare().unwrap();
    assert_eq!(compile_count(), before + 2);
    write_json(
        &cache.join(prepared_resource::PACKAGE_LIST),
        &json!([owner]),
    )
    .unwrap();
    assert_eq!(prepare().unwrap(), archive);
    assert_eq!(runs(root, &build(root, &[])), 6);
    assert_eq!(runs(root, &build(root, &[])), 6);
    assert_eq!(compile_count(), before + 3);
    let disabled = build(root, &["--no-default-features"]);
    assert!(
        disabled.status.success(),
        "{}",
        String::from_utf8_lossy(&disabled.stderr)
    );
    assert_eq!(compile_count(), before + 3);
    assert!(Command::new(root.join("target/debug/consumer"))
        .arg("1")
        .status()
        .unwrap()
        .success());
    assert_eq!(runs(root, &build(root, &[])), 6);
    fs::write(&header, "#define DEVICE_ABI 19\n").unwrap();
    data["config_sha256"] = json!(hash(&header).unwrap());
    write_json(&context, &data).unwrap();
    let stale = build(root, &[]);
    assert!(!stale.status.success());
    assert!(String::from_utf8_lossy(&stale.stderr).contains("stale native resource inputs"));
    assert_eq!(compile_count(), before + 3);
    let updated = prepare().unwrap();
    assert_ne!(updated, archive);
    assert_eq!(runs(root, &build(root, &[])), 7);
    assert_eq!(compile_count(), before + 5);
    assert!(Command::new(root.join("target/debug/consumer"))
        .arg("20")
        .status()
        .unwrap()
        .success());

    let bucket = updated.parent().unwrap();
    let moved = root.join("moved-archive");
    fs::rename(bucket, &moved).unwrap();
    std::os::unix::fs::symlink(&moved, bucket).unwrap();
    let linked = build(root, &[]);
    assert!(!linked.status.success());
    assert!(String::from_utf8_lossy(&linked.stderr).contains("must not be a symlink"));
    assert!(prepare().unwrap_err().contains("must not be a symlink"));
    fs::remove_file(bucket).unwrap();
    fs::rename(moved, bucket).unwrap();

    fs::write(
        root.join("selected/src/platform/nuttx/ffi/value.c"),
        "#include <context.h>\nint device_value(void) { return CONTEXT_ABI; }\n",
    )
    .unwrap();
    let original = prepare().unwrap();
    let shadowing_header = higher_priority.join("context.h");
    fs::write(&shadowing_header, "#define CONTEXT_ABI 27\n").unwrap();
    assert_ne!(prepare().unwrap(), original);
    runs(root, &build(root, &[]));
    assert!(Command::new(root.join("target/debug/consumer"))
        .arg("27")
        .status()
        .unwrap()
        .success());
    fs::remove_file(shadowing_header).unwrap();
    fs::write(&mutate, "").unwrap();
    fs::write(private.join("context.h"), "#define CONTEXT_ABI 5\n").unwrap();
    assert!(prepare()
        .unwrap_err()
        .contains("changed during compilation"));
    fs::remove_file(mutate).unwrap();
    prepare().unwrap();
    let included = build(root, &["--features", "unused-helper"]);
    assert!(!included.status.success());
    assert!(String::from_utf8_lossy(&included.stderr)
        .contains("unconsumed private helper must not compile"));
    fs::create_dir(root.join(".cargo")).unwrap();
    fs::write(
        root.join(".cargo/config.toml"),
        "[env]\nCPATH = '/fixture/include'\n",
    )
    .unwrap();
    assert!(prepared_resource::cargo_compiler_environment(root).unwrap());
}
