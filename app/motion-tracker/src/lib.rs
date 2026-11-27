//! Motion observations and the outputs selected by this application build.
#![forbid(unsafe_code)]

mod clock;
mod communications;
mod health;
mod preferences;
mod runtime;
mod screen;
mod text;
mod tracker;

use preferences::Interval;
use std::io::{self, Write};

#[derive(Default)]
struct ReportOptions {
    interval: Option<Interval>,
    frames: Option<u32>,
    usb_stream: bool,
}

impl ReportOptions {
    fn parse(&mut self, arg: &str, args: &mut impl Iterator<Item = String>) -> io::Result<bool> {
        match arg {
            "--interval-ms" => {
                self.interval = Some(
                    args.next()
                        .and_then(|value| value.parse().ok())
                        .and_then(Interval::new)
                        .ok_or(io::ErrorKind::InvalidInput)?,
                );
            }
            "--frames" => {
                self.frames = Some(
                    args.next()
                        .and_then(|value| value.parse().ok())
                        .filter(|count| *count > 0)
                        .ok_or(io::ErrorKind::InvalidInput)?,
                );
            }
            "--stream-usb" => self.usb_stream = true,
            _ => return Ok(false),
        }
        Ok(true)
    }
}

fn preferences(requested: Option<Interval>) -> io::Result<Interval> {
    let result = settings::open().and_then(|mut store| match requested {
        Some(interval) => {
            // Only the explicit settings command writes flash.
            if Interval::load(&mut store) != Ok(interval) {
                interval.save(&mut store)?;
            }
            Ok(interval)
        }
        None => Interval::load(&mut store),
    });
    match result {
        Ok(interval) => Ok(interval),
        Err(settings::Error::Unsupported) => Ok(requested.unwrap_or(Interval::DEFAULT)),
        Err(error) if requested.is_none() => {
            log::warn!("motion-tracker settings: {error:?}, using defaults");
            Ok(Interval::DEFAULT)
        }
        Err(error) => Err(io::Error::other(format!(
            "save tracker settings: {error:?}"
        ))),
    }
}

pub fn run(args: impl Iterator<Item = String>) -> std::process::ExitCode {
    // A host may already have installed a logger. Standalone commands use stderr.
    let _ = logging::install_stderr(log::LevelFilter::Info);
    match runtime::run(args) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            let _ = writeln!(io::stderr().lock(), "{}: {error}", runtime::USAGE);
            std::process::ExitCode::FAILURE
        }
    }
}
