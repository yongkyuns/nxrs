mod build_esp_dl;
mod compile_model;
#[path = "../../platform/detector-build.rs"]
mod product;
#[path = "../../platform/hal-build.rs"]
mod selection;
use std::{env, path::PathBuf};
fn main() {
    selection::configure();
    let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap()).join("../..");
    let budget = product::configure(&root);
    if let Some(config) = &budget {
        let pico = env::var_os("CARGO_FEATURE_PICO_MODEL").is_some();
        let vww = env::var_os("CARGO_FEATURE_PERSON_MODEL").is_some();
        assert!(
            matches!(
                (config.model.as_str(), pico, vww),
                ("pico", true, false) | ("vww", false, true)
            ),
            "Detector dependencies do not match the product profile. Use the product build frontend, which includes its selected model automatically."
        );
    }
    let value = budget.as_ref().map_or("None".to_owned(), |b| {
        format!(
            "Some(crate::ExecutionBudget {{ stack_bytes: {}, sample_interval_ms: {} }})",
            b.stack_bytes, b.sample_ms
        )
    });
    std::fs::write(
        PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("execution_budget.rs"),
        format!("pub(super) const BUDGET: Option<crate::ExecutionBudget> = {value};\n"),
    )
    .unwrap();
    compile_model::main();
    if env::var_os("CARGO_FEATURE_PICO_MODEL").is_none() {
        return;
    }
    let directory = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let root = directory.parent().unwrap().parent().unwrap();
    for name in ["NXRS_PICO_SDK", "CXX", "AR", "NXRS_NUTTX_INCLUDE"] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    for path in [
        directory.join("src/compute/esp_dl/ffi"),
        root.join("upstream/esp-dl"),
    ] {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    let sdk = env::var_os("NXRS_PICO_SDK")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("target/pico-sdk"));
    println!("cargo:rerun-if-changed={}", sdk.display());
    build_esp_dl::build(
        root,
        &sdk,
        &PathBuf::from(env::var_os("OUT_DIR").unwrap()),
        &env::var("TARGET").unwrap(),
    )
    .unwrap_or_else(|error| panic!("PICO SDK build failed: {error}"));
}
