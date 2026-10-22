fn main() {
    println!("cargo:rerun-if-env-changed=NXRS_ADMISSION_NATIVE_LIB");
    println!("cargo:rerun-if-env-changed=NXRS_ADMISSION_CASES");
    if std::env::var_os("CARGO_FEATURE_QUALIFICATION").is_some() {
        let path = std::env::var("NXRS_ADMISSION_NATIVE_LIB")
            .expect("private qualification SDK library required");
        let path = std::path::Path::new(&path)
            .canonicalize()
            .expect("SDK library directory");
        assert!(
            path.is_dir()
                && path
                    .to_str()
                    .unwrap()
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"/_-.:".contains(&b))
        );
        println!("cargo:rustc-link-search=native={}", path.display());
        println!("cargo:rustc-link-lib=static=nxrs_mbedtls_abi");
        println!("cargo:rustc-link-lib=static=mbedcrypto");
    }
}
