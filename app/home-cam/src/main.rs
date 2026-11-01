//! Home camera entry; native startup details belong to the selected platform.
#![forbid(unsafe_code)]

#[cfg(any(target_os = "nuttx", test))]
mod commissioning;
#[cfg(target_os = "nuttx")]
mod runtime;

fn main() {
    #[cfg(target_os = "nuttx")]
    let result = runtime::run();
    #[cfg(not(target_os = "nuttx"))]
    let result: Result<(), String> = Err("home-cam requires a qualified device platform".into());
    if let Err(error) = result {
        eprintln!("CAMERA_FAIL error={error}");
        std::process::exit(1);
    }
}
