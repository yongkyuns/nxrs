mod build_sdk;
#[path = "../../platform/hal-build.rs"]
mod selection;
use std::{env, path::PathBuf};
fn main() {
    if selection::configure() == "none" {
        return;
    }
    let directory = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    for name in [
        "NXRS_TLS_ARCHIVE",
        "NXRS_NUTTX_INCLUDE",
        "CC",
        "AR",
        "CFLAGS",
        "CMAKE",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    for path in [
        directory.join("src/sdk/ffi"),
        directory.join("build_sdk.rs"),
        directory.join("../../upstream/mbedtls/source.json"),
        directory.join("../../tests/tls/ffi"),
    ] {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    build_sdk::build(&directory, &out, &env::var("TARGET").unwrap())
        .unwrap_or_else(|e| panic!("TLS SDK build: {e}"));
}
