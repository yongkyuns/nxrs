use crate::{ReportOptions, tracker, wire};
use messaging::bounded;
use navigation::observations::{ObservationService, Reading};
use std::io::{self, Write};
use std::sync::mpsc::RecvTimeoutError;
use std::time::Instant;

pub(super) const USAGE: &str =
    "motion_tracker [--interval-ms 100..2000] [--frames N] [--stream-usb]";

pub(super) fn run(mut args: impl Iterator<Item = String>) -> io::Result<()> {
    let mut options = ReportOptions::default();
    while let Some(arg) = args.next() {
        if !options.parse(&arg, &mut args)? {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, arg));
        }
    }
    let interval = crate::preferences(options.interval)?.duration();
    let mut sensors = ObservationService::<1>::new(tracker::IMU_FRESH, None);
    let (sender, inbox) = bounded(32);
    sensors
        .start_imu(0, composition::imu::primary_imu(), sender, |event| event)
        .map_err(|fault| io::Error::other(format!("open IMU: {fault:?}")))?;

    let mut output = options.usb_stream.then(io::stdout);
    let mut remaining = options.frames;
    let mut deadline = Instant::now();
    let result = loop {
        let now = Instant::now();
        if now >= deadline {
            sensors.reap_finished(tracker::report_fault);
            let snapshot = sensors.snapshot(now);
            if let Some(output) = &mut output {
                let mut writer = output.lock();
                if let Err(error) = wire::frame(&snapshot)
                    .and_then(|frame| writer.write_all(frame.bytes()))
                    .and_then(|()| writer.flush())
                {
                    break Err(error);
                }
            }
            if matches!(snapshot.imu[0], Reading::Fault) {
                break Err(io::Error::other("sensor acquisition failed"));
            }
            if let Some(count) = &mut remaining {
                *count -= 1;
                if *count == 0 {
                    let state = tracker::reading_status(&snapshot.imu[0]);
                    eprintln!("motion-tracker: IMU 1 {state}");
                    break if matches!(snapshot.imu[0], Reading::Live(_)) {
                        Ok(())
                    } else {
                        Err(io::Error::other("IMU never reported a fresh sample"))
                    };
                }
            }
            deadline = now + interval;
        }
        match inbox.wait(Some(deadline.saturating_duration_since(Instant::now()))) {
            Ok(event) => {
                if let Some(fault) = sensors.observe(event) {
                    tracker::report_fault(fault);
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break Err(io::Error::other("IMU stopped")),
        }
    };

    let mut failed = false;
    let mut report = |fault| {
        failed = true;
        tracker::report_fault(fault);
    };
    sensors.request_stop(&mut report);
    sensors.join(report);
    result.and(if failed {
        Err(io::Error::other("stop sensor acquisition"))
    } else {
        Ok(())
    })
}
