use crate::{
    charts, communications, export::Capture, screen, tracker, uplink, wire, ReportOptions,
};
use battery::Device as _;
use charts::{History, SAMPLE_INTERVAL};
use communications::{Communications, Pairing, View};
use messaging::bounded;
use navigation::observations::{Observation, ObservationService};
use screen::{Action, Dashboard, Screen};
use std::io::{self, Write};
use std::sync::{
    mpsc::{RecvTimeoutError, TrySendError},
    Arc,
};
use std::time::Instant;
use touch::Session as _;
use tracker::{report_fault, Snapshot, FIX_FRESH, IMU_FRESH};

struct Options {
    reports: ReportOptions,
    ble: bool,
    battery: bool,
    cellular: Option<uplink::Config>,
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
            (Some(address), Some(pin)) if apn.is_some() => {
                Some(uplink::Collector::parse(&address, &pin)?)
            }
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "--collector and --collector-pin require each other and --cellular-apn",
                ));
            }
        };
        options.cellular = apn.map(|apn| uplink::Config { apn, collector });
        if options.battery
            && (options.reports.interval.is_some()
                || options.reports.frames.is_some()
                || options.reports.usb_stream
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

pub(super) const USAGE: &str = "motion_tracker [--interval-ms 100..2000] [--frames N] [--stream-usb] [--ble] [--cellular-apn APN [--collector IPv4:PORT --collector-pin SHA256]] or motion_tracker --battery";

pub(super) fn run(args: impl Iterator<Item = String>) -> io::Result<()> {
    let options = Options::parse(args)?;
    if options.battery {
        return report_battery();
    }
    let interval = crate::preferences(options.reports.interval)?.duration();
    let mut screen = Screen::new(display::open().map_err(display_error)?).map_err(display_error)?;
    let mut dashboard = Dashboard::default();
    let mut history = History::new();
    let (events, inbox) = bounded(32);
    let notify_links = {
        let events = events.clone();
        move || {
            // The current view remains available if the inbox is full.
            let _ = events.try_send(Event::LinkChanged);
        }
    };
    let capture = Arc::new(Capture::default());
    let sensor_event = {
        let capture = capture.clone();
        move |event: Observation| {
            if let Some(sample) = event.measurement() {
                capture.record(sample);
            }
            Event::Sensor(event)
        }
    };
    let mut communications = (options.ble || options.cellular.is_some())
        .then(|| {
            Communications::start(
                options.ble,
                options.cellular,
                capture.clone(),
                notify_links.clone(),
            )
        })
        .transpose()?;
    let mut view = View::default();
    let mut sensors = ObservationService::<2>::new(IMU_FRESH, Some(FIX_FRESH));
    for result in [
        sensors.start_imu(
            0,
            composition::imu::primary_imu(),
            events.clone(),
            sensor_event.clone(),
        ),
        sensors.start_imu(
            1,
            composition::imu::secondary(),
            events.clone(),
            sensor_event.clone(),
        ),
        sensors.start_gnss(
            composition::gnss::primary_gnss(),
            events.clone(),
            sensor_event.clone(),
        ),
    ] {
        if let Err(fault) = result {
            report_fault(fault);
        }
    }
    let mut missed = false;
    let mut touch = touch::open(move |event| {
        match events.try_send(Event::Touch { event, missed }) {
            Ok(()) => missed = false,
            Err(TrySendError::Full(_)) => missed = true,
            Err(TrySendError::Disconnected(_)) => return false,
        }
        true
    })
    .inspect_err(|error| eprintln!("motion-tracker touch: {error:?}"))
    .ok();

    let mut serial = options.reports.usb_stream.then(io::stdout);
    let mut reported_drops = [0; 3];
    let mut rendered = 0_u32;
    let mut next = Instant::now();
    let mut next_plot = next;
    let mut redraw = true;
    let mut links_changed = false;
    let result = loop {
        let now = Instant::now();
        let report_due = now >= next;
        let plot_due = now >= next_plot;
        if links_changed || report_due || plot_due {
            links_changed = false;
            if let Some(links) = &communications {
                let update = links.view(view);
                if update != view {
                    redraw = true;
                    if let Some(key) = update.key {
                        if let Err(error) = dashboard.show_code(&key) {
                            break Err(display_error(error));
                        }
                    } else {
                        dashboard.hide_code();
                    }
                    view = update;
                }
            }
        }
        if report_due {
            if serial.is_some() {
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
        }
        if report_due {
            let snapshot = Snapshot::from(sensors.snapshot(now));
            if let Some(serial) = &mut serial {
                let mut writer = serial.lock();
                let written = wire::frame(&snapshot)
                    .and_then(|frame| writer.write_all(frame.bytes()))
                    .and_then(|()| writer.flush());
                if let Err(error) = written {
                    break Err(error);
                }
            }
            if let Some(links) = &communications {
                links.offer(snapshot);
            }
            if options.reports.frames.is_some() {
                rendered += 1;
                if options.reports.frames == Some(rendered) {
                    eprintln!(
                        "motion-tracker: {rendered} frames, interval={} ms, IMU 1 {}, IMU 2 {}, GNSS {}",
                        interval.as_millis(),
                        tracker::reading_status(&snapshot.imu[0]),
                        tracker::reading_status(&snapshot.imu[1]),
                        tracker::reading_status(&snapshot.gnss),
                    );
                    // Preserve a failing status for bring-up scripts.
                    use tracker::Reading::{Fault, Live};
                    break if !matches!(snapshot.imu[0], Live(_))
                        || !matches!(snapshot.imu[1], Live(_))
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
            next = now + interval;
        }
        if plot_due || redraw {
            let snapshot = Snapshot::from(sensors.snapshot(now));
            if plot_due {
                history.record(&snapshot, now);
                next_plot = now + SAMPLE_INTERVAL;
            }
            if let Err(error) = screen.render(&dashboard, view.link, &snapshot, &history, now) {
                break Err(display_error(error));
            }
            redraw = false;
            let finished = Instant::now();
            if finished >= next_plot {
                // Skip the missed paint and leave time to receive sensor and touch events.
                next_plot = finished + SAMPLE_INTERVAL;
            }
        }
        let wake = next.min(next_plot);
        match inbox.wait(Some(wake.saturating_duration_since(Instant::now()))) {
            Ok(Event::LinkChanged) => links_changed = true,
            Ok(Event::Sensor(event)) => {
                if let Some(fault) = sensors.observe(event) {
                    report_fault(fault);
                }
            }
            Ok(Event::Touch { event, missed }) => {
                if missed {
                    dashboard.cancel_touch();
                }
                match screen.input(&mut dashboard, view.link, event) {
                    Some(Action::Pair) => match &communications {
                        Some(links) => links.pair(Pairing::Start),
                        None => match Communications::start(
                            true,
                            None,
                            capture.clone(),
                            notify_links.clone(),
                        ) {
                            Ok(links) => communications = Some(links),
                            Err(error) => eprintln!("motion-tracker pairing: {error}"),
                        },
                    },
                    Some(Action::CancelPairing) => {
                        if let Some(links) = &communications {
                            links.pair(Pairing::Cancel);
                        }
                    }
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
    let stop_links = communications.as_mut().map(Communications::stop);
    for (operation, failed) in [
        ("stop touch", failed(stop_touch)),
        ("join touch", failed(join_touch)),
        ("stop communications", failed(stop_links)),
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
    result
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
