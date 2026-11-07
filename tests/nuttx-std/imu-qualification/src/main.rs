//! Observe one bounded acquisition window through the public IMU HAL.
#![forbid(unsafe_code)]

use imu::{AccelRange, Config, DeviceError, Event, GyroRange, Imu};
use messaging::{bounded, DeliveryError, Rejected};
use std::{
    process::ExitCode,
    sync::mpsc::{RecvTimeoutError, TrySendError},
    time::{Duration, Instant},
};

const WINDOW: Duration = Duration::from_secs(60);
const NO_STREAM: Duration = Duration::from_secs(3);
const CONFIG: Config = Config {
    min_rate_millihz: 100_000,
    max_rate_millihz: 104_000,
    accel_range: AccelRange::G4,
    gyro_range: GyroRange::Dps2000,
    max_delivery_interval_us: 10_000,
};

#[derive(Default)]
struct Observations {
    samples: u64,
    last: u64,
    delivery_gaps: u64,
    sequence_regressions: u64,
}

impl Observations {
    fn record(&mut self, sequence: u64) {
        self.samples += 1;
        if sequence <= self.last {
            self.sequence_regressions += 1;
        } else {
            self.delivery_gaps += sequence - self.last - 1;
            self.last = sequence;
        }
    }
}

fn report(
    reason: &str,
    rate: u32,
    elapsed: Duration,
    seen: Observations,
    dropped: u32,
    fault: Option<DeviceError>,
) -> ExitCode {
    let pass = reason == "duration_complete"
        && seen.samples > 0
        && seen.delivery_gaps == 0
        && seen.sequence_regressions == 0
        && dropped == 0
        && fault.is_none();
    let status = if pass { "pass" } else { "fail" };
    let fault = fault.map_or_else(|| "null".to_owned(), |error| format!("\"{error:?}\""));
    println!(
        "{{\"schema\":1,\"status\":\"{status}\",\"reason\":\"{reason}\",\
         \"rate_millihz\":{rate},\"window_ms\":{},\"elapsed_ms\":{},\"samples\":{},\
         \"delivery_gaps\":{},\"sequence_regressions\":{},\"hal_dropped\":{dropped},\
         \"fault\":{fault}}}",
        WINDOW.as_millis(),
        elapsed.as_millis(),
        seen.samples,
        seen.delivery_gaps,
        seen.sequence_regressions,
    );
    if pass {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}

fn main() -> ExitCode {
    let (sender, inbox) = bounded(64);
    let sink = move |event| {
        sender.try_send(event).map_err(|error| match error {
            TrySendError::Full(value) => Rejected {
                value,
                error: DeliveryError::Full,
            },
            TrySendError::Disconnected(value) => Rejected {
                value,
                error: DeliveryError::Closed,
            },
        })
    };
    let mut seen = Observations::default();
    let (mut device, applied) = match imu::open_configured(CONFIG, sink) {
        Ok(opened) => opened,
        Err(error) => return report("open_failed", 0, Duration::ZERO, seen, 0, Some(error)),
    };

    let start = Instant::now();
    let end = start + WINDOW;
    let mut stream_deadline = start + NO_STREAM;
    let (reason, fault) = loop {
        let now = Instant::now();
        if now >= end {
            break ("duration_complete", None);
        }
        if now >= stream_deadline {
            break ("no_stream", None);
        }
        let timeout = end.min(stream_deadline) - now;
        match inbox.wait(Some(timeout)) {
            Ok(Event::Sample(sample)) => {
                let received = Instant::now();
                if received >= end {
                    break ("duration_complete", None);
                }
                seen.record(sample.sequence);
                stream_deadline = received + NO_STREAM;
            }
            Ok(Event::Failed(error)) => break ("acquisition_fault", Some(error)),
            Err(RecvTimeoutError::Disconnected) => break ("disconnected", None),
            Err(RecvTimeoutError::Timeout) => {}
        }
    };
    let elapsed = start.elapsed();
    let stopped = device.request_stop();
    let joined = device.join();
    let fault = joined.err().or(stopped.err()).or(fault);
    report(
        reason,
        applied.rate_millihz,
        elapsed,
        seen,
        device.dropped(),
        fault,
    )
}

#[cfg(test)]
mod tests {
    use super::Observations;

    #[test]
    fn counts_startup_and_in_window_delivery_gaps_without_rewinding() {
        let mut seen = Observations::default();
        for sequence in [2, 3, 6, 5, 7] {
            seen.record(sequence);
        }
        assert_eq!(seen.samples, 5);
        assert_eq!(seen.delivery_gaps, 3);
        assert_eq!(seen.sequence_regressions, 1);
    }
}
