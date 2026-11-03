use std::{env, path::PathBuf, process::Command};

fn main() {
    for name in ["NXRS_EVENT_PATH_BUILD_ID", "NXRS_STD_CHANNEL_CLOCK_SOURCE"] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    let identity =
        env::var("NXRS_EVENT_PATH_BUILD_ID").unwrap_or_else(|_| "unrecorded-host".into());
    assert!(
        identity == "unrecorded-host"
            || (identity.len() == 64 && identity.bytes().all(|b| b.is_ascii_hexdigit())),
        "invalid build identity"
    );
    println!("cargo:rustc-env=NXRS_EVENT_PATH_BUILD_ID={identity}");
    let source = env::var("NXRS_STD_CHANNEL_CLOCK_SOURCE").unwrap_or_else(|_| "baseline".into());
    assert!(matches!(source.as_str(), "baseline" | "native-counter"));
    println!("cargo:rustc-env=NXRS_STD_CHANNEL_CLOCK_SOURCE={source}");
    // The shared native adapter is read-only. Target compilation belongs to the
    // existing generic firmware builder, using the target's generated headers.
    if env::var("CARGO_CFG_TARGET_OS").unwrap() == "nuttx" {
        return;
    }
    assert_eq!(env::var("CARGO_CFG_TARGET_OS").unwrap(), "linux");
    assert_eq!(env::var("HOST").unwrap(), env::var("TARGET").unwrap());
    let native = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap())
        .join("../../std-channel-perf/native.c");
    let out = PathBuf::from(env::var("OUT_DIR").unwrap());
    println!("cargo:rerun-if-changed={}", native.display());
    println!(
        "cargo:rerun-if-changed={}",
        native.with_extension("h").display()
    );
    for command in [
        Command::new("cc")
            .args([
                "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread", "-c",
            ])
            .arg(&native)
            .arg("-o")
            .arg(out.join("native.o")),
        Command::new("ar")
            .arg("crs")
            .arg(out.join("libevent_path_native.a"))
            .arg(out.join("native.o")),
    ] {
        assert!(
            command.status().expect("native compiler").success(),
            "{command:?}"
        );
    }
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=event_path_native");
    println!("cargo:rustc-link-lib=pthread");
}
