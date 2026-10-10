// Host functional tests reuse the same native adapter; target C is supplied
// by the final NuttX build using its own configured headers and compiler.
use std::{env, path::PathBuf, process::Command};
fn run(command: &mut Command) {
    assert!(command.status().expect("host C tool invocation").success());
}
fn main() {
    println!("cargo:rerun-if-env-changed=CC");
    println!("cargo:rerun-if-env-changed=AR");
    println!("cargo:rerun-if-changed=pulse_snapshot.h");
    if env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("nuttx") { return; }
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let mut objects = Vec::new();
    for (index, source) in ["runtime.c", "hal_host.c", "../service-footprint/native_thread.c"].iter().enumerate() {
        println!("cargo:rerun-if-changed={source}");
        let object = out.join(format!("native_{index}.o"));
        run(Command::new(env::var("CC").unwrap_or_else(|_| "cc".into()))
            .args(["-std=c11", "-D_DEFAULT_SOURCE", "-O2", "-Wall", "-Wextra", "-Werror", "-c", source])
            .arg("-o").arg(&object));
        objects.push(object);
    }
    println!("cargo:rerun-if-changed=qualification.h");
    run(Command::new(env::var("AR").unwrap_or_else(|_| "ar".into()))
        .arg("crs").arg(out.join("libsq_native.a")).args(objects));
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=sq_native");
    println!("cargo:rustc-link-lib=pthread");
    println!("cargo:rustc-link-lib=rt");
}
