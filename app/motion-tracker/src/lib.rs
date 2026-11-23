//! Motion observations and the outputs selected by this application build.
#![forbid(unsafe_code)]

#[cfg(feature = "dashboard")]
mod charts;
#[cfg(feature = "dashboard")]
mod communications;
#[cfg(feature = "dashboard")]
mod export;
#[cfg(feature = "dashboard")]
#[path = "dashboard.rs"]
mod mode;
#[cfg(not(feature = "dashboard"))]
#[path = "headless.rs"]
mod mode;
mod preferences;
#[cfg(feature = "dashboard")]
mod radio;
#[cfg(feature = "dashboard")]
mod screen;
mod text;
mod tracker;
#[cfg(feature = "dashboard")]
mod uplink;
mod wire;

use preferences::Interval;
use std::io;

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
            eprintln!("motion-tracker settings: {error:?}, using defaults");
            Ok(Interval::DEFAULT)
        }
        Err(error) => Err(io::Error::other(format!(
            "save tracker settings: {error:?}"
        ))),
    }
}

pub fn run(args: impl Iterator<Item = String>) -> std::process::ExitCode {
    match mode::run(args) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{}: {error}", mode::USAGE);
            std::process::ExitCode::FAILURE
        }
    }
}
