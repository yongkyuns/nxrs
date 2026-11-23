use crate::communications::{Cellular, Collector, Outputs, Reports, View};
use crate::{charts, screen, tracker, ReportOptions};
use battery::Device as _;
use charts::{History, SAMPLE_INTERVAL};
use messaging::bounded;
use navigation::observations::{Observation, ObservationService, SensorFault};
use screen::{Action, Dashboard, Screen};
use std::io::{self, Write};
use std::sync::mpsc::{RecvTimeoutError, TrySendError};
use std::time::Instant;
use touch::Session as _;
use tracker::{report_fault, FIX_FRESH, IMU_FRESH};

struct Options {
    reports: ReportOptions,
    ble: bool,
    battery: bool,
    cellular: Option<Cellular>,
}

impl Options {
    fn parse(mut args: impl Iterator<Item = String>) -> io::Result<Self> {
        let mut options = Self {
            reports: ReportOptions::default(),
            ble: false,
            battery: false,
            cellular: None,
        };
        let mut apn = None;
        let mut collector = None;
        let mut pin = None;
        while let Some(arg) = args.next() {
            if options.reports.parse(&arg, &mut args)? {
                continue;
            }
            let value = |args: &mut dyn Iterator<Item = String>| {
                args.next().ok_or(io::ErrorKind::InvalidInput)
            };
            match arg.as_str() {
                "--ble" => options.ble = true,
                "--cellular-apn" => apn = Some(value(&mut args)?),
                "--collector" => collector = Some(value(&mut args)?),
                "--collector-pin" => pin = Some(value(&mut args)?),
                "--battery" => options.battery = true,
                _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, arg)),
            }
        }
        let collector = match (collector, pin) {
            (None, None) => None,
            (Some(address), Some(pin)) if apn.is_some() => Some(Collector::parse(&address, &pin)?),
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "--collector and --collector-pin require each other and --cellular-apn",
                ));
            }
        };
        options.cellular = apn.map(|apn| Cellular { apn, collector });
        if options.battery
            && (options.reports.interval.is_some()
                || options.reports.frames.is_some()
                || options.reports.usb_stream
                || options.reports.archive.is_some()
                || options.ble
                || options.cellular.is_some())
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "--battery must be used on its own",
            ));
        }
        Ok(options)
    }
}

enum Event {
    Sensor(Observation),
    LinkChanged,
    Touch { event: touch::Event, missed: bool },
}

pub(super) const USAGE: &str = "motion_tracker [--interval-ms 100..2000] [--frames N] [--stream-usb] [--archive PATH MAX_BYTES] [--ble] [--cellular-apn APN [--collector IPv4:PORT --collector-pin SHA256]] or motion_tracker --battery";

pub(super) fn run(args: impl Iterator<Item = String>) -> io::Result<()> {
    let options = Options::parse(args)?;
    if options.battery {
        return report_battery();
    }
    let interval = crate::preferences(options.reports.interval)?.duration();
    let mut display = match display::open() {
        Ok(device) => Some((
            Screen::new(device).map_err(display_error)?,
            Dashboard::default(),
            History::new(),
        )),
        Err(display::Error::Unsupported) => None,
        Err(error) => return Err(display_error(error)),
    };
    if options.ble && display.is_none() {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "BLE QR pairing requires a display",
        ));
    }
    let (events, inbox) = bounded(32);
    let notify_links = {
        let events = events.clone();
        move || {
            // The current view remains available if the inbox is full.
            let _ = events.try_send(Event::LinkChanged);
        }
    };
    let mut view = View::default();
    let mut sensors = ObservationService::<2>::new(IMU_FRESH, Some(FIX_FRESH));
    let required = sensors
        .start_gnss(
            composition::gnss::primary_gnss(),
            events.clone(),
            Event::Sensor,
        )
        .and_then(|()| {
            sensors.start_imu(
                0,
                composition::imu::primary_imu(),
                events.clone(),
                Event::Sensor,
            )
        });
    if let Err(fault) = required {
        sensors.request_stop(report_fault);
        sensors.join(report_fault);
        return Err(io::Error::other(format!("open required sensor: {fault:?}")));
    }
    let secondary = composition::imu::optional::secondary().is_some_and(|device| {
        available(sensors.start_imu(1, device, events.clone(), Event::Sensor))
    });
    let mut reports = match Reports::start(
        interval,
        secondary,
        Outputs {
            usb: options.reports.usb_stream,
            archive: options.reports.archive,
            pair_on_start: options.ble,
            cellular: options.cellular,
        },
        notify_links.clone(),
    ) {
        Ok(reports) => reports,
        Err(error) => {
            sensors.request_stop(report_fault);
            sensors.join(report_fault);
            return Err(error);
        }
    };
    let mut touch = display.as_ref().and_then(|_| {
        let mut missed = false;
        match touch::open(move |event| {
            match events.try_send(Event::Touch { event, missed }) {
                Ok(()) => missed = false,
                Err(TrySendError::Full(_)) => missed = true,
                Err(TrySendError::Disconnected(_)) => return false,
            }
            true
        }) {
            Ok(session) => Some(session),
            Err(touch::Error::Unsupported) => None,
            Err(error) => {
                eprintln!("motion-tracker touch: {error:?}");
                None
            }
        }
    });

    let mut reported_drops = [0; 3];
    let mut rendered = 0_u32;
    let mut next_plot = reports.deadline();
    let mut redraw = true;
    let mut links_changed = false;
    let result = loop {
        let now = Instant::now();
        let report_due = now >= reports.deadline();
        let plot_due = display.is_some() && now >= next_plot;
        if links_changed || report_due || plot_due {
            links_changed = false;
            let update = reports.view(view);
            if update != view {
                redraw = true;
                if let Some((_, dashboard, _)) = &mut display {
                    if let Some(key) = update.key {
                        if let Err(error) = dashboard.show_code(&key) {
                            break Err(display_error(error));
                        }
                    } else {
                        dashboard.hide_code();
                    }
                }
                view = update;
            }
        }
        if report_due {
            if options.reports.usb_stream {
                let ([primary, secondary], gnss) = sensors.dropped();
                for ((name, count), reported) in ["IMU 1", "IMU 2", "GNSS"]
                    .into_iter()
                    .zip([primary, secondary, gnss])
                    .zip(&mut reported_drops)
                {
                    if let Some(count) = count.filter(|count| *count > *reported) {
                        eprintln!("motion-tracker {name} queue drops: {count}");
                        *reported = count;
                    }
                }
            }
            sensors.reap_finished(report_fault);
            if touch.as_ref().is_some_and(touch::Session::is_finished) {
                let mut finished = touch.take().unwrap();
                eprintln!("motion-tracker touch stopped: {:?}", finished.join());
            }
            let snapshot = match reports.publish(&sensors, now) {
                Ok(snapshot) => snapshot,
                Err(error) => break Err(error),
            };
            if options.reports.frames.is_some() {
                rendered += 1;
                if options.reports.frames == Some(rendered) {
                    eprintln!(
                        "motion-tracker: {rendered} frames, interval={} ms, IMU 1 {}, IMU 2 {}, GNSS {}",
                        interval.as_millis(),
                        tracker::status(snapshot.imu[0]),
                        tracker::status(snapshot.imu[1]),
                        tracker::reading_status(&snapshot.gnss),
                    );
                    // Preserve a failing status for bring-up scripts.
                    use tracker::Reading::{Fault, Live};
                    break if !matches!(snapshot.imu[0], Some(Live(_)))
                        || snapshot.imu[1].is_some_and(|reading| !matches!(reading, Live(_)))
                        || matches!(snapshot.gnss, Fault)
                    {
                        Err(io::Error::other(
                            "sensor acquisition failed or an IMU never reported",
                        ))
                    } else {
                        Ok(())
                    };
                }
            }
        }
        if let Some((screen, dashboard, history)) = display.as_mut().filter(|_| plot_due || redraw)
        {
            let snapshot = match reports.snapshot(&sensors, now) {
                Ok(snapshot) => snapshot,
                Err(error) => break Err(error),
            };
            if plot_due {
                history.record(&snapshot, now);
                next_plot = now + SAMPLE_INTERVAL;
            }
            if let Err(error) = screen.render(dashboard, view.link, &snapshot, history, now) {
                break Err(display_error(error));
            }
            redraw = false;
            let finished = Instant::now();
            if finished >= next_plot {
                // Skip the missed paint and leave time to receive sensor and touch events.
                next_plot = finished + SAMPLE_INTERVAL;
            }
        }
        let wake = if display.is_some() {
            reports.deadline().min(next_plot)
        } else {
            reports.deadline()
        };
        match inbox.wait(Some(wake.saturating_duration_since(Instant::now()))) {
            Ok(Event::LinkChanged) => links_changed = true,
            Ok(Event::Sensor(event)) => {
                if let Some(fault) = sensors.observe(event) {
                    report_fault(fault);
                }
            }
            Ok(Event::Touch { event, missed }) => {
                let Some((screen, dashboard, _)) = &mut display else {
                    continue;
                };
                if missed {
                    dashboard.cancel_touch();
                }
                match screen.input(dashboard, view.link, event) {
                    Some(Action::Pair) => {
                        if let Err(error) = reports.pair(notify_links.clone()) {
                            eprintln!("motion-tracker pairing: {error}");
                        }
                    }
                    Some(Action::CancelPairing) => reports.cancel_pairing(),
                    Some(Action::Redraw) => redraw = true,
                    None => {}
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                break Err(io::Error::other("tracker event inbox closed"));
            }
        }
    };

    // Ask all workers to stop before waiting for any one join.
    let mut sensor_failed = false;
    let mut report = |fault| {
        sensor_failed = true;
        report_fault(fault);
    };
    sensors.request_stop(&mut report);
    let stop_touch = touch.as_mut().map(touch::Session::request_stop);
    sensors.join(report);
    let join_touch = touch.as_mut().map(touch::Session::join);
    let finish_reports = reports.finish();
    for (operation, failed) in [
        ("stop touch", failed(stop_touch)),
        ("join touch", failed(join_touch)),
    ] {
        if failed {
            eprintln!("motion-tracker: {operation} failed");
            if result.is_ok() {
                return Err(io::Error::other(operation));
            }
        }
    }
    if sensor_failed && result.is_ok() {
        return Err(io::Error::other("stop sensor acquisition"));
    }
    result.and(finish_reports)
}

fn available(result: Result<(), SensorFault>) -> bool {
    match result {
        Err(SensorFault::Imu(_, imu::DeviceError::Unsupported)) => false,
        Err(fault) => {
            report_fault(fault);
            true
        }
        Ok(()) => true,
    }
}

fn failed<T>(result: Option<Result<(), T>>) -> bool {
    result.is_some_and(|result| result.is_err())
}

fn display_error(error: display::Error) -> io::Error {
    io::Error::other(format!("tracker display: {error:?}"))
}

fn report_battery() -> io::Result<()> {
    let status = composition::battery::primary_battery()
        .status()
        .map_err(|error| io::Error::other(format!("battery status: {error:?}")))?;
    let input = if status.external_power {
        "external power present"
    } else {
        "external power absent"
    };
    let mut output = io::stdout().lock();
    match status.battery {
        Some(battery) => match battery.percent {
            Some(percent) => writeln!(
                output,
                "Battery {percent}% ({:?}, {input})",
                battery.charge_state
            ),
            None => writeln!(
                output,
                "Battery estimate unavailable ({:?}, {input})",
                battery.charge_state
            ),
        },
        None => writeln!(output, "No battery detected ({input})"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cellular_collector_requires_an_address_pin_and_apn() {
        let pin = "aB".repeat(32);
        let parse = |args: &[&str]| Options::parse(args.iter().map(ToString::to_string));
        assert!(parse(&["--collector", "192.0.2.3:1234", "--collector-pin", &pin]).is_err());
        assert!(parse(&["--cellular-apn", "example", "--collector", "192.0.2.3:1234"]).is_err());
        assert!(parse(&["--cellular-apn", "example", "--collector-pin", &pin]).is_err());

        let launch = |address: &str, pin: &str| {
            parse(&[
                "--cellular-apn",
                "example",
                "--collector",
                address,
                "--collector-pin",
                pin,
            ])
        };
        assert!(launch("192.0.2.3:0", &pin).is_err());
        assert!(launch("192.0.2.3:1234", &"00".repeat(32)).is_err());
        assert!(launch("192.0.2.3:1234", &"é".repeat(32)).is_err());
        let collector = launch("192.0.2.3:1234", &pin)
            .unwrap()
            .cellular
            .unwrap()
            .collector
            .unwrap();
        assert_eq!(collector.pin, [0xab; 32]);
        assert!(parse(&["--cellular-apn", "example"])
            .unwrap()
            .cellular
            .unwrap()
            .collector
            .is_none());
    }
}
