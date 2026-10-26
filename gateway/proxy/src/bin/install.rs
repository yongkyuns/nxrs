//! Explicit operator installation of an already reviewed public proxy pair.
#![forbid(unsafe_code)]
fn main() {
    if let Err(category) = nxrs_gateway_proxy::install_pair() {
        eprintln!("Proxy pair installation refused ({category}).");
        std::process::exit(1);
    }
}
