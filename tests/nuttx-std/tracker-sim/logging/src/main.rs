//! Exercise an independent boot-started archive task in the headless image.
#![deny(unsafe_code)]

#[cfg(target_os = "nuttx")]
mod syslog;

use std::fs::File;
use std::io;
use std::num::NonZeroU64;
use std::path::Path;
use std::process::ExitCode;

fn archive(mut args: impl Iterator<Item = String>) -> io::Result<()> {
    let source = args.next().ok_or(io::ErrorKind::InvalidInput)?;
    let active = args.next().ok_or(io::ErrorKind::InvalidInput)?;
    let limit = args
        .next()
        .and_then(|arg| arg.parse::<NonZeroU64>().ok())
        .ok_or(io::ErrorKind::InvalidInput)?;
    if args.next().is_some() {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    logging::archive(File::open(source)?, Path::new(&active), limit)
}

fn main() -> ExitCode {
    // The archive task and repeated tracker commands share this logger.
    #[cfg(target_os = "nuttx")]
    let _ = syslog::install(log::LevelFilter::Info);
    let mut args = std::env::args().skip(1).peekable();
    if args.peek().map(String::as_str) != Some("--drain-system-log") {
        return nxrs_motion_tracker::run(args);
    }
    args.next();
    match archive(args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("system log archive: {error}");
            ExitCode::FAILURE
        }
    }
}
