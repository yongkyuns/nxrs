use std::{env, path::PathBuf, process::Command};

fn checked(command: &mut Command) {
    assert!(
        command.status().expect("run native compiler").success(),
        "{command:?}"
    );
}

fn main() {
    println!("cargo:rerun-if-env-changed=NXRS_STD_CHANNEL_HOOK_STAGE");
    let stage = env::var("NXRS_STD_CHANNEL_HOOK_STAGE").unwrap_or_else(|_| "0".into());
    assert!(
        matches!(stage.as_str(), "0" | "1" | "2"),
        "invalid hook stage"
    );
    println!("cargo:rustc-env=NXRS_STD_CHANNEL_HOOK_STAGE={stage}");
    println!("cargo:rerun-if-env-changed=NXRS_STD_CHANNEL_BUILD_ID");
    let build_id =
        env::var("NXRS_STD_CHANNEL_BUILD_ID").unwrap_or_else(|_| "unrecorded-host".into());
    assert!(
        build_id == "unrecorded-host"
            || (build_id.len() == 64 && build_id.bytes().all(|b| b.is_ascii_hexdigit())),
        "invalid build identity"
    );
    println!("cargo:rustc-env=NXRS_STD_CHANNEL_BUILD_ID={build_id}");
    println!("cargo:rerun-if-env-changed=NXRS_STD_CHANNEL_CLOCK_SOURCE");
    let clock_source =
        env::var("NXRS_STD_CHANNEL_CLOCK_SOURCE").unwrap_or_else(|_| "baseline".into());
    assert!(
        matches!(clock_source.as_str(), "baseline" | "native-counter"),
        "invalid clock source"
    );
    println!("cargo:rustc-env=NXRS_STD_CHANNEL_CLOCK_SOURCE={clock_source}");
    let os = env::var("CARGO_CFG_TARGET_OS").unwrap();
    if os == "nuttx" {
        // The firmware builder compiles native.c against its generated headers.
        // Never link a host object into the target image.
        return;
    }
    assert_eq!(
        os, "linux",
        "only native Linux functional runs are supported"
    );
    assert_eq!(env::var("HOST").unwrap(), env::var("TARGET").unwrap());
    let source = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap()).join("../native.c");
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
            .arg(out.join("libstd_channel_perf_native.a"))
            .arg(out.join("native.o")),
    );
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=std_channel_perf_native");
    println!("cargo:rustc-link-lib=pthread");
}
