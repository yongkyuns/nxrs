//! Home camera entry; native startup details belong to the selected platform.
#![forbid(unsafe_code)]

mod commissioning;
mod runtime;

fn main() {
    let result = runtime::run();
    if let Err(error) = result {
        eprintln!("CAMERA_FAIL error={error}");
        std::process::exit(1);
    }
}
