//! Synchronous admission of an administrator-approved Linux/amd64 proxy bundle.
//! Root, kernel, filesystem and clock integrity are deployment prerequisites.
#![forbid(unsafe_code)]

fn main() {
    let result = nxrs_gateway_proxy::run();
    if let Err(category) = result {
        // Never print configuration, loader output, paths or private arguments.
        eprintln!("Proxy admission refused ({category}).");
        std::process::exit(1);
    }
}
