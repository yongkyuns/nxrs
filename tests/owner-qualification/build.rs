fn main() {
    println!("cargo:rerun-if-env-changed=OWNER_QUALIFICATION_BUILD_ID");
    let id = std::env::var("OWNER_QUALIFICATION_BUILD_ID").unwrap_or_else(|_| "host".into());
    let target = std::env::var("CARGO_CFG_TARGET_OS").unwrap();
    assert!(
        id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit())
            || target != "nuttx" && id == "host",
        "NuttX builds require the source-bound workflow.py build identity"
    );
    println!("cargo:rustc-env=OWNER_QUALIFICATION_BUILD_ID={id}");
}
