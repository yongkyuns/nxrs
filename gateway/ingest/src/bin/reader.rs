//! Credentialed RTSP demux/remux only; never opens an image decoder.
#![forbid(unsafe_code)]
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if let Err(error) = nxrs_camera_ingest::read_rtsp(&args) {
        eprintln!("nxrs-rtsp-reader: {error:?}");
        std::process::exit(1);
    }
}
