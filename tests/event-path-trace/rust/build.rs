use std::env;

fn main() {
    for name in ["NXRS_EVENT_TRACE_BUILD_ID", "NXRS_STD_CHANNEL_CLOCK_SOURCE"] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    let identity =
        env::var("NXRS_EVENT_TRACE_BUILD_ID").unwrap_or_else(|_| "unrecorded-host".into());
    assert!(
        identity == "unrecorded-host"
            || (identity.len() == 64 && identity.bytes().all(|c| c.is_ascii_hexdigit()))
    );
    println!("cargo:rustc-env=NXRS_EVENT_TRACE_BUILD_ID={identity}");
    println!("cargo:rustc-env=NXRS_STD_CHANNEL_CLOCK_SOURCE=native-counter");
}
