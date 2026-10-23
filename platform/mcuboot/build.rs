use std::path::PathBuf;
fn main() {
    println!("cargo:rustc-check-cfg=cfg(nxrs_boot_policy_bound)");
    println!("cargo:rerun-if-env-changed=NXRS_BOOT_TRUST_RUST");
    if let Some(value) = std::env::var_os("NXRS_BOOT_TRUST_RUST") {
        let path = PathBuf::from(value);
        assert!(path.is_absolute() && path.canonicalize().unwrap() == path);
        let info = std::fs::metadata(&path).unwrap();
        assert!(info.is_file() && info.len() > 0 && info.len() <= 8192);
        println!("cargo:rerun-if-changed={}", path.display());
        println!("cargo:rustc-cfg=nxrs_boot_policy_bound");
    }
}
