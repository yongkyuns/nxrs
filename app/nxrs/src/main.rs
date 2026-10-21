//! App-owned entry: configured camera -> recording and telemetry workflows.
//! Device construction is delegated to capability-local HAL facades; service wiring is Rust.
#![forbid(unsafe_code)]

use std::io;
use std::sync::mpsc::RecvTimeoutError;
use std::time::{Duration, Instant};

use nxrs_applications::{CameraProduct, ConsumerReport, Error};
use camera::{DeviceError, Format, PixelFormat};
use camera_service::Error as CameraError;
use recording_service::Error as RecordingError;
use telemetry_service::Error as TelemetryError;
use event::{bounded, EventInbox};

const FRAME_BYTES: usize = 65_536;
const HISTORY: usize = 2;
const MAX_SOURCE_BYTES: usize = 64 * 1024 * 1024;
const BUSY_RETRY: Duration = Duration::from_millis(1);

// Startup allocation uses std directly; no HAL provider is needed.
fn frame_buffer(bytes: usize) -> io::Result<Box<[u8]>> {
    let mut buffer = Vec::new();
    buffer
        .try_reserve_exact(bytes)
        .map_err(|_| io::Error::other("frame buffer allocation failed"))?;
    buffer.resize(bytes, 0);
    Ok(buffer.into_boxed_slice())
}

fn checked<T, E: std::fmt::Debug>(result: Result<T, E>) -> io::Result<T> {
    result.map_err(|error| io::Error::other(format!("portable service: {error:?}")))
}

fn progress<T>(result: Result<T, Error>) -> io::Result<()> {
    match result {
        Ok(_) => Ok(()),
        Err(error) if is_busy(&error) => Ok(()),
        Err(error) => Err(io::Error::other(format!("portable service: {error:?}"))),
    }
}

fn busy<T>(result: &Result<T, Error>) -> bool {
    matches!(result, Err(error) if is_busy(error))
}

fn is_busy(error: &Error) -> bool {
    matches!(error,
        Error::Camera(CameraError::Device(DeviceError::Busy))
            | Error::Recording(RecordingError::Device(storage::DeviceError::Busy))
            | Error::Telemetry(TelemetryError::Device(transport::DeviceError::Busy)))
}

fn consumer_busy(report: &ConsumerReport) -> bool {
    busy(&report.recorder) || busy(&report.monitor)
}

/// One canonical owner wait point. An admitted event requests shutdown; timeout
/// means whichever scheduled deadline expired should be handled by the owner.
fn wait_owner(inbox: &EventInbox<()>, timeout: Duration) -> io::Result<bool> {
    match inbox.wait(Some(timeout)) {
        Ok(()) => Ok(true),
        Err(RecvTimeoutError::Timeout) => Ok(false),
        Err(RecvTimeoutError::Disconnected) => Err(io::Error::new(
            io::ErrorKind::BrokenPipe,
            "product owner control inbox disconnected",
        )),
    }
}

fn timeout_until(now_ms: u64, deadline_ms: u64) -> Duration {
    Duration::from_millis(deadline_ms.saturating_sub(now_ms))
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() != 7 {
        return Err("usage: nxrs <packed.raw> <width> <height> <gray8|rgb565> <period-ms> <new-output-dir> <udp-address:port>".into());
    }
    let format = Format {
        width: args[1].parse()?,
        height: args[2].parse()?,
        pixels: match args[3].as_str() {
            "gray8" => PixelFormat::Gray8,
            "rgb565" => PixelFormat::Rgb565,
            _ => return Err("only packed gray8 and rgb565 are supported".into()),
        },
    };
    if !format.fits(FRAME_BYTES) {
        return Err("frame exceeds the native example's 65536-byte capacity".into());
    }

    let period_ms: u64 = args[4].parse()?;
    let (camera, frames) = camera::replay(&args[0], format, period_ms, MAX_SOURCE_BYTES)?;
    let deadline_ms = frames
        .checked_mul(period_ms)
        .and_then(|n| n.checked_add(10_000))
        .ok_or("replay duration overflow")?;
    let transport = transport::open(&args[6])?;
    let storage = storage::open(&args[5], 4, FRAME_BYTES, frames)?;

    // Payloads are allocated directly in the heap. Boxing an already-built
    // inline product would still permit large startup stack temporaries.
    let history = [frame_buffer(FRAME_BYTES)?, frame_buffer(FRAME_BYTES)?];
    let staging = frame_buffer(FRAME_BYTES)?;
    let mut product = checked(
        CameraProduct::<_, _, _, FRAME_BYTES, HISTORY, _>::with_buffers(
            camera, storage, transport, history, staging,
        ),
    )?;
    checked(product.camera.start(format))?;
    checked(product.recorder.start(&product.camera))?;
    checked(product.monitor.start(&product.camera))?;

    // Keep the replay epoch at workflow startup; service inputs remain
    // owner-relative milliseconds.
    let origin = Instant::now();
    let now_ms = || u64::try_from(origin.elapsed().as_millis()).unwrap_or(u64::MAX);

    // The main thread is the product owner. Control producers may be added
    // without changing the wait topology; keeping this sender alive also makes
    // disconnect distinct from a requested shutdown.
    let (_shutdown, inbox) = bounded::<()>(1);

    let mut camera_due = true;
    let mut consumers_due = true;
    let mut camera_polls = 0u64;
    let mut timed_waits = 0u64;
    let mut busy_retries = 0u64;

    let work_result = (|| -> io::Result<()> {
        loop {
            let now = now_ms();

            if camera_due {
                camera_polls = camera_polls.saturating_add(1);
                progress(product.poll_camera(now))?;
            }

            let sinks_busy = if camera_due || consumers_due {
                let report = product.process_consumers();
                let busy = consumer_busy(&report);
                progress(report.recorder)?;
                progress(report.monitor)?;
                busy
            } else {
                false
            };

            let recorder = product.recorder.stats();
            let monitor = product.monitor.stats();
            if recorder.processed + recorder.skipped == frames
                && monitor.processed + monitor.skipped == frames
            {
                if recorder.skipped != 0 || monitor.skipped != 0 {
                    return Err(io::Error::other(
                        "consumer overrun: increase history or reduce source rate",
                    ));
                }
                return Ok(());
            }

            let now = now_ms();
            if now >= deadline_ms {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "native product progress timed out",
                ));
            }

            let camera_deadline = product.camera.next_poll_at_ms();
            let consumer_deadline = sinks_busy.then(|| {
                let retry_ms = u64::try_from(BUSY_RETRY.as_millis()).unwrap_or(1);
                now.saturating_add(retry_ms.max(1))
            });

            if camera_deadline.is_none() && consumer_deadline.is_none() {
                return Err(io::Error::other(
                    "camera made no progress and advertised no retry deadline/readiness",
                ));
            }

            let mut next_deadline = deadline_ms;
            if let Some(deadline) = camera_deadline {
                next_deadline = next_deadline.min(deadline);
            }
            if let Some(deadline) = consumer_deadline {
                next_deadline = next_deadline.min(deadline);
            }

            if next_deadline <= now {
                camera_due = camera_deadline.is_some_and(|deadline| deadline <= now);
                consumers_due =
                    consumer_deadline.is_some_and(|deadline| deadline <= now);
                if consumers_due {
                    busy_retries = busy_retries.saturating_add(1);
                }
                continue;
            }

            timed_waits = timed_waits.saturating_add(1);
            if wait_owner(&inbox, timeout_until(now, next_deadline))? {
                return Err(io::Error::new(
                    io::ErrorKind::Interrupted,
                    "product owner shutdown requested",
                ));
            }

            let after = now_ms();
            camera_due = camera_deadline.is_some_and(|deadline| deadline <= after);
            consumers_due =
                consumer_deadline.is_some_and(|deadline| deadline <= after);
            if consumers_due {
                busy_retries = busy_retries.saturating_add(1);
            }
        }
    })();

    let cleanup_deadline = Instant::now() + Duration::from_secs(10);
    let cleanup_result = (|| -> io::Result<()> {
        loop {
            let report = product.shutdown();
            if let Err(error) = report.camera {
                return Err(io::Error::other(format!("camera shutdown: {error:?}")));
            }
            match report.recorder {
                Ok(()) => return Ok(()),
                Err(error) if is_busy(&error) && Instant::now() < cleanup_deadline => {
                    timed_waits = timed_waits.saturating_add(1);
                    busy_retries = busy_retries.saturating_add(1);
                    let remaining = cleanup_deadline.saturating_duration_since(Instant::now());
                    if wait_owner(&inbox, BUSY_RETRY.min(remaining))? {
                        return Err(io::Error::new(
                            io::ErrorKind::Interrupted,
                            "product owner shutdown requested during cleanup",
                        ));
                    }
                }
                Err(error) => {
                    return Err(io::Error::other(format!(
                        "recording flush: {error:?}"
                    )));
                }
            }
        }
    })();

    drop(product); // flush succeeded; close submission and detach the now-idle filesystem worker
    work_result?;
    cleanup_result?;

    println!(
        "Runtime owner: camera_polls={camera_polls} timed_waits={timed_waits} busy_retries={busy_retries}"
    );
    println!("Recorded {frames} frames; submitted {frames} UDP summaries; zero skipped frames.");
    println!("Committed recording directory: {}", args[5]);
    Ok(())
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("nxrs: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{frame_buffer, wait_owner};
    use event::bounded;
    use std::time::Duration;

    #[test]
    fn startup_buffers_are_heap_backed_and_allocation_failure_is_reported() {
        std::thread::Builder::new()
            .stack_size(64 * 1024)
            .spawn(|| {
                let buffers = [
                    frame_buffer(65_536).unwrap(),
                    frame_buffer(65_536).unwrap(),
                    frame_buffer(65_536).unwrap(),
                ];
                assert!(buffers
                    .iter()
                    .all(|b| b.len() == 65_536 && b.iter().all(|v| *v == 0)));
                assert!(frame_buffer(usize::MAX).is_err());
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn owner_wait_is_bounded_and_wakes_for_shutdown() {
        let (shutdown, inbox) = bounded(1);
        shutdown.try_send(()).unwrap();
        assert!(shutdown.try_send(()).is_err());
        assert!(wait_owner(&inbox, Duration::from_secs(1)).unwrap());
    }

    #[test]
    fn owner_wait_times_out_without_polling() {
        let (_shutdown, inbox) = bounded::<()>(1);
        assert!(!wait_owner(&inbox, Duration::from_millis(1)).unwrap());
    }
}
