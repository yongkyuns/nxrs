fn main() {
    println!("cargo:rustc-check-cfg=cfg(nxrs_device_identity_qualification)");
}
