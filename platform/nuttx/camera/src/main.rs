//! Signed camera product entry; qualification commands live under tests/.
#![forbid(unsafe_code)]
fn main() {
    if let Err(error) = camera_firmware::run() {
        eprintln!("CAMERA_FAIL error={error}");
        std::process::exit(1);
    }
}
