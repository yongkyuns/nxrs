fn main() {
    println!("cargo:rerun-if-env-changed=MPSC_BUILD_ID");
    println!("cargo:rerun-if-env-changed=MPSC_CLOCK_HZ");
    let hz = clock();
    assert!(matches!(hz, 1000000 | 240000000));
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    std::fs::write(
        out.join("clock.rs"),
        format!("pub const CLOCK_HZ: u32 = {hz};"),
    )
    .unwrap();
    let id = std::env::var("MPSC_BUILD_ID").unwrap_or_else(|_| "host".into());
    assert!(id == "host" || id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit()));
    println!("cargo:rustc-env=MPSC_BUILD_ID={id}");
}

fn clock() -> u32 {
    std::env::var("MPSC_CLOCK_HZ")
        .map(|s| s.parse().expect("clock integer"))
        .unwrap_or(240000000)
}
