//! Qualify single-device observation and independent system-log archival.
#![deny(unsafe_code)]

#[cfg(target_os = "nuttx")]
mod syslog;

use navigation::observations::{ObservationService, Reading};
use std::fs::File;
use std::io;
use std::io::Write;
use std::num::NonZeroU64;
use std::path::Path;
use std::process::ExitCode;
use std::sync::mpsc::RecvTimeoutError;
use std::time::{Duration, Instant};

fn observe(mut args: impl Iterator<Item = String>) -> io::Result<()> {
    let mut interval = Duration::from_millis(500);
    let mut frames = 5;
    let mut stream = false;
    while let Some(arg) = args.next() {
        let mut number = || -> io::Result<u64> {
            args.next()
                .and_then(|value| value.parse().ok())
                .filter(|value| *value != 0)
                .ok_or(io::ErrorKind::InvalidInput.into())
        };
        match arg.as_str() {
            "--interval-ms" => interval = Duration::from_millis(number()?),
            "--frames" => frames = number()?,
            "--stream-usb" => stream = true,
            _ => return Err(io::ErrorKind::InvalidInput.into()),
        }
    }
    // The archive and this task share the installed log facade on NuttX.
    let (sender, inbox) = messaging::bounded(32);
    let mut sensor = ObservationService::<1>::new(Duration::from_millis(500), None);
    sensor
        .start_imu(0, composition::imu::primary_imu(), sender, |event| event)
        .map_err(|error| io::Error::other(format!("open IMU: {error:?}")))?;
    let result = (|| {
        let mut deadline = Instant::now();
        loop {
            let now = Instant::now();
            if now >= deadline {
                let reading = sensor.snapshot(now).imu[0];
                if stream {
                    let mut output = io::stdout().lock();
                    match reading {
                        Reading::Live(sample) => {
                            let [ax, ay, az] = sample.accel_mps2;
                            writeln!(output, "{{\"v\":2,\"imu\":[{{\"state\":\"live\",\"seq\":{},\"accel_mps2\":[{ax},{ay},{az}]}}]}}", sample.sequence)?;
                        }
                        Reading::Waiting => {
                            writeln!(output, "{{\"v\":2,\"imu\":[{{\"state\":\"waiting\"}}]}}")?
                        }
                        Reading::Stale => {
                            writeln!(output, "{{\"v\":2,\"imu\":[{{\"state\":\"stale\"}}]}}")?
                        }
                        Reading::Fault => return Err(io::Error::other("IMU failed")),
                    }
                    output.flush()?;
                }
                frames -= 1;
                if frames == 0 {
                    if !matches!(reading, Reading::Live(_)) {
                        return Err(io::Error::other("IMU never reported a fresh reading"));
                    }
                    println!("motion-tracker: IMU 1 live");
                    return Ok(());
                }
                deadline = now + interval;
            }
            match inbox.wait(Some(deadline.saturating_duration_since(Instant::now()))) {
                Ok(event) => {
                    if sensor.observe(event).is_some() {
                        return Err(io::Error::other("IMU failed"));
                    }
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => return Err(io::Error::other("IMU stopped")),
            }
        }
    })();
    let mut failed = false;
    sensor.request_stop(|_| failed = true);
    sensor.join(|_| failed = true);
    result.and(if failed {
        Err(io::Error::other("stop IMU"))
    } else {
        Ok(())
    })
}

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
    #[cfg(target_os = "nuttx")]
    syslog::install(log::LevelFilter::Info).map_err(io::Error::other)?;
    logging::archive(File::open(source)?, Path::new(&active), limit)
}

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1).peekable();
    let result = if args.peek().map(String::as_str) == Some("--drain-system-log") {
        args.next();
        archive(args)
    } else {
        observe(args)
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("system log archive: {error}");
            ExitCode::FAILURE
        }
    }
}
