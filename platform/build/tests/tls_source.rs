//! Verify missing/corrupt archives are rejected before invoking any SDK tool or creating output.
#[path = "../../../hal/tls/build_sdk.rs"]
mod sdk;
#[test]
fn verified_source_precedes_sdk_work() {
    let temp = nxrs_build::TempDir::new(&std::env::temp_dir()).unwrap();
    let archive = temp.0.join("release.tar.bz2");
    let out = temp.0.join("out");
    let previous = std::env::var_os("NXRS_TLS_ARCHIVE");
    std::env::set_var("NXRS_TLS_ARCHIVE", &archive);
    let here = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../hal/tls");
    for content in [None, Some(b"corrupt release".as_slice())] {
        if let Some(bytes) = content {
            std::fs::write(&archive, bytes).unwrap();
        }
        let error = sdk::build(&here, &out, "x86_64-unknown-linux-gnu").unwrap_err();
        assert!(error.contains("Verified TLS source archive required"));
        assert!(!out.exists());
    }
    match previous {
        Some(value) => std::env::set_var("NXRS_TLS_ARCHIVE", value),
        None => std::env::remove_var("NXRS_TLS_ARCHIVE"),
    }
}
