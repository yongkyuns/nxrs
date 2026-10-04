fn main() {
    // App-owned stack reservation; do not modify the upstream HAL checkout.
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    std::fs::copy("stack.x", out.join("stack.x")).unwrap();
    println!("cargo:rustc-link-search={}", out.display());
    println!("cargo:rerun-if-changed=stack.x");
}
