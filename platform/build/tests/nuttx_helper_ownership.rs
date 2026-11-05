//! Real Cargo/CC witness for private helper ownership and incremental headers.
//! The host C ABI is intentional here; firmware ABI remains a target-build gate.
#![cfg(unix)]
use nxrs_build::{hash, json::json, read_json, write_json, TempDir};
use std::{fs, path::Path, process::Command};

fn build(root: &Path, extra: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO"))
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
        .env_remove("CARGO_BUILD_TARGET")
        .env_remove("RUSTFLAGS")
        .env_remove("CARGO_ENCODED_RUSTFLAGS")
        .output()
        .unwrap()
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
                && m["package_id"]
                    .as_str()
                    .unwrap()
                    .contains("selected-helper")
        })
        .unwrap();
    assert!(script["linked_libs"]
        .as_array()
        .unwrap()
        .contains(&json!("static=selected_helper_native")));
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
    for (directory, package) in [("selected", "selected-helper"), ("unused", "unused-helper")] {
        let path = root.join(directory);
        fs::create_dir_all(path.join("src/platform/nuttx/ffi")).unwrap();
        fs::write(path.join("Cargo.toml"),format!("[package]\nname={package:?}\nversion=\"0.0.0\"\nedition=\"2021\"\n[build-dependencies]\nnxrs-build-support={{path={support:?}}}\n")).unwrap();
        fs::write(path.join("src/lib.rs"),"extern \"C\" { fn device_value() -> i32; }\npub fn value() -> i32 { unsafe { device_value() } }\n").unwrap();
        fs::write(path.join("build.rs"),r#"
use std::{env,io::Write,path::PathBuf};
fn main() {
    let here=PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let out=PathBuf::from(env::var_os("OUT_DIR").unwrap());
    for line in nxrs_build_support::native::build(&here,"nuttx","fixture-nuttx",&out).unwrap() { println!("{line}"); }
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
    fs::write(root.join("consumer/Cargo.toml"),"[package]\nname=\"consumer\"\nversion=\"0.0.0\"\nedition=\"2021\"\n[dependencies]\nselected-helper={path=\"../selected\"}\nunused-helper={path=\"../unused\",optional=true}\n").unwrap();
    fs::write(
        root.join("consumer/src/main.rs"),
        "fn main() { assert!(selected_helper::value() > 0); }\n",
    )
    .unwrap();
    let include = root.join("include");
    fs::create_dir_all(include.join("nuttx")).unwrap();
    let header = include.join("nuttx/config.h");
    fs::write(&header, "#define DEVICE_ABI 7\n").unwrap();
    let private = root.join("private architecture #$");
    fs::create_dir(&private).unwrap();
    fs::write(private.join("context.h"), "#define CONTEXT_ABI 3\n").unwrap();
    let context = root.join("native.json");
    write_json(
        &context,
        &json!({"schema":1,"target":"fixture-nuttx","directory":root,
        "include":include,"config_sha256":hash(&header).unwrap(),"cc":["cc"],"ar":["ar","rcs"],
        "cflags":["-I",include.to_str().unwrap(),"-I",private.to_str().unwrap()]}),
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
    fs::write(
        root.join("selected/src/platform/nuttx/ffi/value.c"),
        "#include <nuttx/config.h>\nint device_value(void) { return DEVICE_ABI + 1; }\n",
    )
    .unwrap();
    assert_eq!(runs(root, &build(root, &[])), 4);
    let included = build(root, &["--features", "unused-helper"]);
    assert!(!included.status.success());
    assert!(String::from_utf8_lossy(&included.stderr)
        .contains("unconsumed private helper must not compile"));
}
