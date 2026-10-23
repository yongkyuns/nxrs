fn main() {
    println!("cargo:rerun-if-env-changed=NXRS_EMBASSY_CASE");
    let name = std::env::var("NXRS_EMBASSY_CASE").unwrap_or_else(|_| "wire".into());
    let (mode, cooperative) = match name.as_str() {
        "wire" => ("Wire", false),
        "wire-cooperative" => ("Wire", true),
        "packet" => ("Packet", false),
        "packet-cooperative" => ("Packet", true),
        "baseline" => ("Baseline", false),
        _ => panic!("unknown Embassy experiment: {name}"),
    };
    let selected = format!(
        "pub const MODE: Mode = Mode::{mode};\npub const COOPERATIVE: bool = {cooperative};\npub const FILTER_WORDS: usize = if matches!(MODE, Mode::Packet) {{ 3 }} else {{ 0 }};\n"
    );
    let generated = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    std::fs::write(generated.join("embassy_case.rs"), selected).unwrap();
    // App-owned stack reservation; do not modify the upstream HAL checkout.
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    std::fs::copy("stack.x", out.join("stack.x")).unwrap();
    println!("cargo:rustc-link-search={}", out.display());
    println!("cargo:rerun-if-changed=stack.x");
}
