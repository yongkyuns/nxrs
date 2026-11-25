use crate::{communications, screen, tracker, ReportOptions};
use battery::Device as _;
use communications::{Capture, CellularConfig, Communications, Endpoint, Pairing, Telemetry, View};
use messaging::bounded;
use navigation::observations::{Observation, ObservationService};
use screen::{Action, Screen};
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
    cellular: Option<CellularConfig>,
    tile_relay: Option<Endpoint>,
    zoom: Option<crate::map::Zoom>,
}

impl Options {
    fn parse(mut args: impl Iterator<Item = String>) -> io::Result<Self> {
        let mut options = Self {
            reports: ReportOptions::default(),
            ble: false,
            battery: false,
            cellular: None,
            tile_relay: None,
            zoom: None,
        };
        let mut apn = None;
        let mut collector = None;
        let mut pin = None;
        let mut tile_relay = None;
        let mut tile_pin = None;
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
                "--map-relay" => tile_relay = Some(value(&mut args)?),
                "--map-pin" => tile_pin = Some(value(&mut args)?),
                "--map-zoom" => {
                    options.zoom = Some(
                        value(&mut args)?
                            .parse()
                            .ok()
                            .and_then(crate::map::Zoom::new)
                            .ok_or_else(|| {
                                io::Error::new(
                                    io::ErrorKind::InvalidInput,
                                    "--map-zoom expects 15 through 18",
                                )
                            })?,
                    );
                }
                "--battery" => options.battery = true,
                _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, arg)),
            }
        }
        let collector = match (collector, pin) {
            (None, None) => None,
            (Some(address), Some(pin)) if apn.is_some() => Some(Endpoint::parse(&address, &pin)?),
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "--collector and --collector-pin require each other and --cellular-apn",
                ));
            }
        };
        options.tile_relay = match (tile_relay, tile_pin) {
            (None, None) => None,
            (Some(address), Some(pin)) => Some(Endpoint::parse(&address, &pin)?),
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "--map-relay and --map-pin require each other",
                ))
            }
        };
        options.cellular = apn.map(|apn| CellularConfig { apn, collector });
        if options.battery
            && (options.reports.interval.is_some()
                || options.reports.frames.is_some()
                || options.reports.usb_stream
                || options.ble
                || options.cellular.is_some()
                || options.tile_relay.is_some()
                || options.zoom.is_some())
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
    SensorChanged,
    LinksUpdated,
    Touch { event: touch::Event, missed: bool },
}

pub(super) const USAGE: &str = "motion_tracker [--interval-ms 100..2000] [--frames N] [--stream-usb] [--ble] [--cellular-apn APN [--collector IPv4:PORT --collector-pin SHA256]] [--map-relay IPv4:PORT --map-pin SHA256] [--map-zoom 15..18] or motion_tracker --battery";

pub(super) fn run(args: impl Iterator<Item = String>) -> io::Result<()> {
    let options = Options::parse(args)?;
    if options.battery {
        return report_battery();
    }
    crate::health::run(|timer| track(options, timer))
}

fn track(
    options: Options,
    mut timer: Option<&mut dyn watchdog::RuntimeWatchdog>,
) -> io::Result<()> {
    let interval = crate::preferences(options.reports.interval)?.duration();
    let mut screen = match display::open() {
        Ok(display) => {
            Some(Screen::new(display, options.zoom.unwrap_or_default()).map_err(display_error)?)
        }
        Err(display::Error::Unsupported) => None,
        Err(error) => return Err(display_error(error)),
    };
    let (events, inbox) = bounded(32);
    let notify_links = {
        let events = events.clone();
        move || {
            // The current view remains available if the inbox is full.
            let _ = events.try_send(Event::LinksUpdated);
        }
    };
    use imu::Device as _;
    let primary = composition::imu::primary_imu();
    let secondary = composition::imu::secondary();
    let primary_scale = primary
        .default_config()
        .and_then(|config| primary.resolve(config));
    let secondary_config = secondary.default_config();
    let secondary_present = !matches!(&secondary_config, Err(imu::DeviceError::Unsupported));
    let secondary_scale = secondary_config.and_then(|config| secondary.resolve(config));
    let capture = Arc::new(Capture::new(
        [primary_scale, secondary_scale]
            .map(|applied| applied.ok().and_then(|config| config.counts_scale)),
    ));
    let (mut sensors, readings) = ObservationService::new_shared(IMU_FRESH, Some(FIX_FRESH));
    let sensor_event = {
        let capture = capture.clone();
        let readings = readings.clone();
        move |event: Observation| {
            match readings.record(&event) {
                Ok(Some(sample)) => capture.record(sample),
                Ok(None) => {}
                Err(_) => {
                    capture.sensor_failed();
                    return Some(Event::SensorChanged);
                }
            }
            None
        }
    };
    let telemetry = Telemetry {
        readings,
        capture,
        interval,
    };
    let mut communications =
        (options.ble || options.cellular.is_some() || options.tile_relay.is_some())
            .then(|| {
                Communications::start(
                    options.ble,
                    options.cellular,
                    options.tile_relay,
                    telemetry.clone(),
                    notify_links.clone(),
                )
            })
            .transpose()?;
    let mut view = View::default();
    let mut required = Ok(());
    for result in [
        sensors.start_imu_with(0, primary, events.clone(), sensor_event.clone()),
        sensors.start_gnss_with(
            composition::gnss::primary_gnss(),
            events.clone(),
            sensor_event.clone(),
        ),
    ] {
        if let Err(fault) = result {
            report_fault(fault);
            required = Err(io::Error::other(format!("open required sensor: {fault:?}")));
        }
    }
    if required.is_ok() && secondary_present {
        if let Err(fault) = sensors.start_imu_with(1, secondary, events.clone(), sensor_event) {
            report_fault(fault);
        }
    }
    let mut missed = false;
    let mut touch = screen
        .is_some()
        .then(|| {
            touch::open(move |event| {
                match events.try_send(Event::Touch { event, missed }) {
                    Ok(()) => missed = false,
                    Err(TrySendError::Full(_)) => missed = true,
                    Err(TrySendError::Disconnected(_)) => return false,
                }
                true
            })
        })
        .and_then(|session| match session {
            Ok(touch) => Some(touch),
            Err(touch::Error::Unsupported) => None,
            Err(error) => {
                log::warn!("motion-tracker touch: {error:?}");
                None
            }
        });

    let mut serial = options.reports.usb_stream.then(io::stdout);
    let mut reported_drops = [0; 3];
    let mut rendered = 0_u32;
    let mut next = Instant::now();
    let mut redraw = true;
    let mut links_changed = false;
    let result = (|| {
        required?;
        // Keep synchronous SPI transfers on the existing display owner at a
        // fixed priority. Acquisition and transport have their own deadlines.
        match screen
            .as_ref()
            .map(|_| execution::configure_current(execution::Priority::Background))
        {
            None | Some(Ok(()) | Err(execution::Error::Unsupported)) => {}
            Some(Err(error)) => {
                return Err(io::Error::other(format!(
                    "tracker display priority: {error:?}"
                )));
            }
        }
        loop {
            let now = Instant::now();
            let report_due = now >= next;
            let plot_due = screen
                .as_ref()
                .is_some_and(|screen| now >= screen.deadline());
            if links_changed || report_due || plot_due {
                links_changed = false;
                if let Some(links) = &communications {
                    let update = links.view(view);
                    if update != view {
                        if let Some(screen) = &mut screen {
                            screen.update_link(update).map_err(display_error)?;
                            redraw = true;
                        }
                    }
                    view = update;
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
                            log::warn!("motion-tracker {name} queue drops: {count}");
                            *reported = count;
                        }
                    }
                }
                sensors.reap_finished(report_fault);
                if touch.as_ref().is_some_and(touch::Session::is_finished) {
                    let mut finished = touch.take().unwrap();
                    let result = finished.join();
                    log::warn!("motion-tracker touch stopped: {result:?}");
                }
            }
            if report_due {
                let snapshot = telemetry.snapshot(now);
                if let Some(serial) = &mut serial {
                    communications::write_usb(&mut serial.lock(), &snapshot)?;
                }
                if options.reports.frames.is_some() {
                    rendered += 1;
                    if options.reports.frames == Some(rendered) {
                        let _ = writeln!(
                        io::stderr().lock(),
                        "motion-tracker: {rendered} frames, interval={} ms, IMU 1 {}, IMU 2 {}, GNSS {}",
                        interval.as_millis(),
                        tracker::reading_status(&snapshot.imu[0]),
                        if secondary_present { tracker::reading_status(&snapshot.imu[1]) } else { "unavailable" },
                        tracker::reading_status(&snapshot.gnss),
                    );
                        // Preserve a failing status for bring-up scripts.
                        use tracker::Reading::{Fault, Live};
                        break if !matches!(snapshot.imu[0], Live(_))
                            || (secondary_present && !matches!(snapshot.imu[1], Live(_)))
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
            if let Some(screen) = screen.as_mut().filter(|_| plot_due || redraw) {
                let snapshot = Snapshot::from(sensors.snapshot(now));
                screen
                    .draw(view.link, &snapshot, now, |wanted| {
                        communications.as_ref().and_then(|links| links.map(wanted))
                    })
                    .map_err(display_error)?;
                redraw = false;
            }
            if report_due {
                if let Err(error) = crate::health::progress(&mut timer) {
                    break Err(error);
                }
            }
            let wake = screen
                .as_ref()
                .map_or(next, |screen| next.min(screen.deadline()));
            match inbox.wait(Some(wake.saturating_duration_since(Instant::now()))) {
                Ok(Event::LinksUpdated) => {
                    links_changed = true;
                    redraw = true;
                }
                Ok(Event::SensorChanged) => redraw = true,
                Ok(Event::Touch { event, missed }) => {
                    match screen
                        .as_mut()
                        .and_then(|screen| screen.input(view.link, event, missed))
                    {
                        Some(Action::Pair) => match &communications {
                            Some(links) => links.pair(Pairing::Start),
                            None => match Communications::start(
                                true,
                                None,
                                None,
                                telemetry.clone(),
                                notify_links.clone(),
                            ) {
                                Ok(links) => communications = Some(links),
                                Err(error) => log::warn!("motion-tracker pairing: {error}"),
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
        }
    })();

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
            log::error!("motion-tracker: {operation} failed");
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
    fn map_relay_requires_an_address_and_pin_without_requiring_a_modem() {
        let parse = |args: &[&str]| Options::parse(args.iter().map(ToString::to_string));
        let pin = "ab".repeat(32);
        assert!(parse(&["--map-relay", "192.0.2.3:8444"]).is_err());
        assert!(parse(&["--map-pin", &pin]).is_err());
        let options = parse(&["--map-relay", "192.0.2.3:8444", "--map-pin", &pin]).unwrap();
        assert!(options.cellular.is_none());
        assert_eq!(options.tile_relay.unwrap().pin, [0xab; 32]);
    }

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
