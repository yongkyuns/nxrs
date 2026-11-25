mod build_sdk;
use std::{env, path::PathBuf};
fn main() {
    if nxrs_build_support::configure_hal().unwrap_or_else(|error| panic!("{error}")) == "none" {
        return;
    }
    let directory = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let root = nxrs_build_support::sdk_root(&directory).unwrap_or_else(|error| panic!("{error}"));
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
        root.join("upstream/mbedtls/source.json"),
    ] {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    build_sdk::build(&directory, &out, &env::var("TARGET").unwrap())
        .unwrap_or_else(|e| panic!("TLS SDK build: {e}"));
}
