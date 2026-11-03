use std::{env, path::PathBuf, process::Command};

fn checked(command: &mut Command) {
    assert!(
        command.status().expect("run native compiler").success(),
        "{command:?}"
    );
}

fn main() {
    println!("cargo:rerun-if-env-changed=NXRS_CHANNEL_PROGRESS_HOOK_STAGE");
    let stage = env::var("NXRS_CHANNEL_PROGRESS_HOOK_STAGE").unwrap_or_else(|_| "0".into());
    assert!(
        matches!(stage.as_str(), "0" | "1" | "2" | "3" | "4"),
        "invalid hook stage"
    );
    println!("cargo:rustc-env=NXRS_CHANNEL_PROGRESS_HOOK_STAGE={stage}");
    println!("cargo:rerun-if-env-changed=NXRS_CHANNEL_PROGRESS_BUILD_ID");
    let build_id =
        env::var("NXRS_CHANNEL_PROGRESS_BUILD_ID").unwrap_or_else(|_| "unrecorded-host".into());
    assert!(
        build_id == "unrecorded-host"
            || (build_id.len() == 64 && build_id.bytes().all(|b| b.is_ascii_hexdigit())),
        "invalid build identity"
    );
    println!("cargo:rustc-env=NXRS_CHANNEL_PROGRESS_BUILD_ID={build_id}");
    let os = env::var("CARGO_CFG_TARGET_OS").unwrap();
    if os == "nuttx" {
        // The target builder compiles this same mechanical ABI against the
        // selected kernel headers. No host object enters a firmware image.
        return;
    }
    assert_eq!(os, "linux", "host functional smoke requires Linux");
    assert_eq!(env::var("HOST").unwrap(), env::var("TARGET").unwrap());
    let source = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap())
        .join("../../std-channel-perf/native.c");
    let out = PathBuf::from(env::var("OUT_DIR").unwrap());
    println!("cargo:rerun-if-changed={}", source.display());
    println!(
        "cargo:rerun-if-changed={}",
        source.with_file_name("native.h").display()
    );
    checked(
        Command::new("cc")
            .args([
                "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread", "-c",
            ])
            .arg(&source)
            .arg("-o")
            .arg(out.join("native.o")),
    );
    checked(
        Command::new("ar")
            .arg("crs")
            .arg(out.join("libchannel_progress_native.a"))
            .arg(out.join("native.o")),
    );
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=channel_progress_native");
    println!("cargo:rustc-link-lib=pthread");
}
