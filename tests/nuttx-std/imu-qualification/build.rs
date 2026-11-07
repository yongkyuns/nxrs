use std::{env, path::PathBuf};

fn main() {
    let target = env::var("TARGET").expect("Cargo TARGET");
    let target_os = env::var("CARGO_CFG_TARGET_OS").expect("Cargo target OS");
    if env::var_os("CARGO_FEATURE_PROBE").is_none() || target_os != "nuttx" {
        return;
    }

    let manifest = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("manifest dir"));
    let source_directory = manifest.join("ffi");
    let sources =
        ["notes.c", "resources.c", "worker.c"].map(|source| source_directory.join(source));
    for source in &sources {
        assert!(
            source.is_file(),
            "missing checked-in fixture source: {}",
            source.display()
        );
        println!("cargo:rerun-if-changed={}", source.display());
    }
    let source_refs = sources.iter().collect::<Vec<_>>();
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR"));
    for directive in nxrs_build_support::native::build_nuttx_sources(
        &source_directory,
        "imu_qualification",
        &target,
        &out,
        &source_refs,
    )
    .unwrap_or_else(|error| panic!("NuttX qualification FFI build: {error}"))
    {
        println!("{directive}");
    }
}
