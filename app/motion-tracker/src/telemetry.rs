//! Owns tracker acquisition and publishes snapshots on the execution owner's deadlines.
#[cfg(not(feature = "ble-link"))]
use crate::clock::Capture;
#[cfg(feature = "ble-link")]
use crate::communications::Capture;
use crate::{
    charts::SAMPLE_INTERVAL,
    tracker::{FIX_FRESH, IMU_FRESH, Snapshot, report_fault},
};
use navigation::observations::{Measurement, Observation, ObservationService};
use std::{
    io,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

type Samples = [Option<Observation>; 3];

struct Output {
    view: Snapshot,
    report: Option<Snapshot>,
}

/// Latest render state plus one optional report for the console or a finite run.
pub(super) struct Reader(Arc<Mutex<Output>>);

impl Reader {
    pub(super) fn read(&self) -> Option<(Snapshot, Option<Snapshot>)> {
        let mut output = self.0.try_lock().ok()?;
        Some((output.view, output.report.take()))
    }
}

pub(super) struct Telemetry {
    sensors: ObservationService<2>,
    capture: Arc<Capture>,
    samples: Arc<Mutex<Samples>>,
    output: Arc<Mutex<Output>>,
    interval: Duration,
    next_report: Instant,
    next_view: Instant,
    local_reports: bool,
    reported_drops: Option<[u32; 3]>,
}

impl Telemetry {
    pub(super) fn start(interval: Duration, local_reports: bool, usb: bool) -> (Self, Reader) {
        let primary = composition::imu::primary_imu();
        let secondary = composition::imu::secondary();
        #[cfg(feature = "ble-link")]
        let capture = {
            use imu::Device as _;
            let primary_scale = primary
                .default_config()
                .and_then(|config| primary.resolve(config));
            let secondary_scale = secondary
                .default_config()
                .and_then(|config| secondary.resolve(config));
            Arc::new(Capture::new([primary_scale, secondary_scale].map(
                |applied| applied.ok().and_then(|config| config.counts_scale),
            )))
        };
        #[cfg(not(feature = "ble-link"))]
        let capture = Arc::new(Capture::new());

        let samples = Arc::new(Mutex::new([None, None, None]));
        let admit = {
            let capture = capture.clone();
            let samples = samples.clone();
            move |event: Observation| {
                let Some(sample) = event.measurement() else {
                    // Invalid frames do not update the view. Terminal faults
                    // remain observable through the HAL's join result.
                    return Ok(());
                };
                capture.record(sample);
                let index = match sample {
                    Measurement::Imu(index, _) => index,
                    Measurement::Gnss(_) => 2,
                };
                samples.lock().unwrap()[index] = Some(event);
                Ok(())
            }
        };
        let mut sensors = ObservationService::new(IMU_FRESH, Some(FIX_FRESH));
        for result in [
            sensors.start_imu_with(0, primary, admit.clone()),
            sensors.start_imu_with(1, secondary, admit.clone()),
            sensors.start_gnss_with(composition::gnss::primary_gnss(), admit),
        ] {
            if let Err(fault) = result {
                report_fault(fault);
            }
        }

        let now = Instant::now();
        let output = Arc::new(Mutex::new(Output {
            view: sensors.snapshot(now).into(),
            report: None,
        }));
        let reader = Reader(output.clone());
        (
            Self {
                sensors,
                capture,
                samples,
                output,
                interval,
                next_report: now,
                next_view: now,
                local_reports,
                reported_drops: usb.then_some([0; 3]),
            },
            reader,
        )
    }

    #[cfg(feature = "ble-link")]
    pub(super) fn capture(&self) -> Arc<Capture> {
        self.capture.clone()
    }

    pub(super) fn deadline(&self) -> Instant {
        self.next_view.min(self.next_report)
    }

    /// Update the display state and return a publication only when a report is due.
    /// The console reader gets its own replaceable copy and a coalesced wake hint.
    pub(super) fn step(&mut self, now: Instant) -> (Option<Snapshot>, bool) {
        if now < self.deadline() {
            return (None, false);
        }
        if let Ok(mut pending) = self.samples.try_lock() {
            let current = std::mem::take(&mut *pending);
            drop(pending);
            for observation in current.into_iter().flatten() {
                if let Some(fault) = self.sensors.observe(observation) {
                    report_fault(fault);
                }
            }
        }
        let report_due = now >= self.next_report;
        if report_due {
            self.report_drops();
            self.next_report = advance(self.next_report, self.interval, now);
        }
        if now >= self.next_view {
            self.next_view = advance(self.next_view, SAMPLE_INTERVAL, now);
        }
        self.sensors.reap_finished(report_fault);
        let mut snapshot = Snapshot::from(self.sensors.snapshot(now));
        snapshot.clock = self.capture.clock();
        let mut output = self.output.lock().unwrap();
        output.view = snapshot;
        let notify = report_due && self.local_reports && output.report.replace(snapshot).is_none();
        (report_due.then_some(snapshot), notify)
    }

    fn report_drops(&mut self) {
        if let Some(reported) = &mut self.reported_drops {
            let ([primary, secondary], gnss) = self.sensors.dropped();
            for ((name, count), previous) in ["IMU 1", "IMU 2", "GNSS"]
                .into_iter()
                .zip([primary, secondary, gnss])
                .zip(reported)
            {
                if let Some(count) = count.filter(|count| *count > *previous) {
                    log::warn!("motion-tracker {name} queue drops: {count}");
                    *previous = count;
                }
            }
        }
    }

    pub(super) fn stop(&mut self) -> io::Result<()> {
        let mut failed = false;
        let mut report = |fault| {
            failed = true;
            report_fault(fault);
        };
        self.sensors.request_stop(&mut report);
        self.sensors.join(report);
        if failed {
            Err(io::Error::other("stop sensor acquisition"))
        } else {
            Ok(())
        }
    }
}

impl Drop for Telemetry {
    fn drop(&mut self) {
        // Also quiesce acquisitions if the selected execution owner fails at startup.
        let _ = self.stop();
    }
}

fn advance(deadline: Instant, period: Duration, now: Instant) -> Instant {
    let next = deadline + period;
    if next > now { next } else { now + period }
}
