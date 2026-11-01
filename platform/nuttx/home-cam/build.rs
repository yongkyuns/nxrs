#[path = "../../detector-build.rs"]
mod product;
fn main() {
    let root =
        std::path::PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap()).join("../../..");
    let config = product::configure(&root);
    let enabled = config.is_some();
    let stack = config.as_ref().map_or(131072, |c| c.stack_bytes);
    let sample = config.as_ref().map_or(750, |c| c.sample_ms);
    std::fs::write(
        std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("detector_budget.rs"),
        format!("pub const ENABLED:bool={enabled};\npub const STACK_BYTES:usize={stack};\npub const SAMPLE_MS:u64={sample};\n"),
    ).unwrap();
}
