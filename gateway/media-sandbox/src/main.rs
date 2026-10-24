//! Single-threaded unprivileged sandbox launcher. Nothing is installed or
//! changed on the host; restrictions apply only to this process/descendants.
#![deny(unsafe_code)]
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
};
#[cfg(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
#[path = "platform/linux/mod.rs"]
mod platform;
#[cfg(not(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
)))]
mod platform {
    pub fn run(
        _: &std::path::Path,
        _: &[std::ffi::OsString],
        _: Option<&std::path::Path>,
    ) -> std::io::Result<()> {
        Err(std::io::ErrorKind::Unsupported.into())
    }
    pub fn check() -> std::io::Result<()> {
        Err(std::io::ErrorKind::Unsupported.into())
    }
}
fn execute() -> Result<(), ()> {
    let arguments: Vec<OsString> = std::env::args_os().skip(1).collect();
    if arguments.len() == 1 && arguments[0] == "--check" {
        platform::check().map_err(|_| ())?;
        println!("NXRS_MEDIA_ISOLATION_READY");
        return Ok(());
    }
    if arguments.is_empty()
        || arguments.len() > 128
        || arguments.iter().map(|a| a.len()).sum::<usize>() > 65536
    {
        return Err(());
    }
    let mut next = arguments.as_slice();
    let work: Option<PathBuf> = if next.first().is_some_and(|arg| arg == "--work") {
        if next.len() < 4 {
            return Err(());
        }
        let directory = PathBuf::from(&next[1]);
        next = &next[2..];
        Some(directory)
    } else {
        None
    };
    if next.len() < 2 || next[0] != "--" {
        return Err(());
    }
    let program = Path::new(&next[1]);
    if !program.is_absolute() {
        return Err(());
    }
    platform::run(program, &next[2..], work.as_deref()).map_err(|_| ())
}
fn main() {
    if execute().is_err() {
        eprintln!("nxrs-media-sandbox: isolation or execution refused");
        std::process::exit(1);
    }
}
