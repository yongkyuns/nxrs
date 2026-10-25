//! Synchronous admission of an administrator-approved Linux/amd64 proxy bundle.
//! Root, kernel, filesystem and clock integrity are deployment prerequisites.
#![forbid(unsafe_code)]

#[cfg(unix)]
mod admission;

fn main() {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    let result = admission::run();
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    let result: Result<(), &str> = Err("unsupported-target");
    if let Err(category) = result {
        // Never print configuration, loader output, paths or private arguments.
        eprintln!("Proxy admission refused ({category}).");
        std::process::exit(1);
    }
}
