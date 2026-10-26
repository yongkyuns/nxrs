//! Synchronous verified proxy launch and explicit reviewed-pair publication.
#![forbid(unsafe_code)]
#[cfg(all(unix, any(test, all(target_os = "linux", target_arch = "x86_64"))))]
mod admission;
#[cfg(all(unix, any(test, all(target_os = "linux", target_arch = "x86_64"))))]
mod install;

pub fn run() -> Result<(), &'static str> {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    {
        admission::run()
    }
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    {
        Err("unsupported-target")
    }
}

pub fn install_pair() -> Result<(), &'static str> {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    {
        install::run()
    }
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    {
        Err("unsupported-target")
    }
}
