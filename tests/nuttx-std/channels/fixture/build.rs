fn main() {
    for key in ["STD_COMPARISON_ID", "STD_COMPARISON_CANDIDATE"] {
        println!("cargo:rerun-if-env-changed={key}");
        let value = std::env::var(key).expect("use the source-bound channels/compare.py workflow");
        assert!(if key == "STD_COMPARISON_ID" {
            value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
        } else {
            matches!(
                value.as_str(),
                "baseline" | "upstream-queue" | "pthread-parker-init" | "no-outlining"
            )
        });
        println!("cargo:rustc-env={key}={value}");
    }
}
