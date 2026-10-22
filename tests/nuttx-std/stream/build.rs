fn main() {
    println!("cargo:rerun-if-env-changed=NXRS_STREAM_MODEL");
    let model = std::env::var("NXRS_STREAM_MODEL").unwrap_or_else(|_| "none".into());
    let (enabled, stack, interval, pico) = match model.as_str() {
        "none" => (false, 128 * 1024, 750, false),
        "pico" => (true, 128 * 1024, 750, true),
        "vww" => (true, 512 * 1024, 2000, false),
        _ => panic!("unknown stream model: {model}; choose none, pico or vww"),
    };
    let probe = std::env::var_os("CARGO_FEATURE_PROBE").is_some();
    let open = if probe && pico {
        "profile::open().map_err(Into::into)"
    } else {
        "detector::open(detector::Config::default()).map_err(Into::into)"
    };
    let context = if pico {
        "unsafe extern \"C\" { fn detector_compute_enabled() -> i32; }\n// SAFETY: qualification reads this CPU's native compute state.\nunsafe { detector_compute_enabled() }"
    } else {
        "0"
    };
    let probes = if probe && pico {
        let root =
            std::path::PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap()).join("src");
        format!("#[path = {:?}] mod profile;\n#[path = {:?}] mod batch;\npub fn batch_command(args:&[String])->Option<Result<(),String>> {{ if args == [\"detect-batch\"] {{ Some(batch::run()) }} else {{ None }} }}\n", root.join("detector_profile.rs"),root.join("detector_batch.rs"))
    } else {
        "pub fn batch_command(_: &[String])->Option<Result<(),String>> { None }\n".into()
    };
    let source = format!("pub const ENABLED:bool = {enabled};\npub const STACK_BYTES:usize = {stack};\npub const SAMPLE_MS:u64 = {interval};\npub fn open() -> Result<impl detector::Detector, detection_service::Error> {{ {open} }}\npub fn compute_state() -> i32 {{ {context} }}\n");
    std::fs::write(
        std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("stream_detector.rs"),
        probes + &source,
    )
    .unwrap();
}
