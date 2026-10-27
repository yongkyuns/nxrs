//! Single-use credentialed publication; image coding happens in its sibling.
#![forbid(unsafe_code)]
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if let Err(error) = nxrs_camera_ingest::publish(&args) {
        eprintln!("nxrs-rtsp-publisher: {error:?}");
        std::process::exit(1);
    }
}
