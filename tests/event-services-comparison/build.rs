#[allow(dead_code)]
mod experiment;
#[allow(dead_code)]
mod stack_config;
fn main() {
    experiment::configure();
    let stack = stack_config::StackConfig::from_environment();
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    std::fs::write(out.join("stack.x"), stack.linker_script()).unwrap();
    std::fs::write(out.join("event_stack.rs"), stack.rust_constants()).unwrap();
    println!("cargo:rustc-link-search={}", out.display());
    println!("cargo:rerun-if-changed=stack_config.rs");
}
