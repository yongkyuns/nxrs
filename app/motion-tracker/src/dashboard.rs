use crate::{ReportOptions, charts, communications, screen, telemetry::Telemetry, tracker, wire};
use battery::Device as _;
use charts::{History, SAMPLE_INTERVAL};
use communications::{CellularConfig, Communications, Endpoint, LinkStatus, Pairing, Update, View};
use messaging::bounded;
use screen::{Action, Dashboard, Screen};
use std::io::{self, Write};
use std::sync::mpsc::{RecvTimeoutError, TrySendError};
use std::time::{Duration, Instant};
use touch::Session as _;

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
        let mut upload_interval = None;
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
                "--cellular-report-secs" => {
                    upload_interval = Some(
                        value(&mut args)?
                            .parse::<u64>()
                            .ok()
                            .filter(|secs| (1..=86_400).contains(secs))
                            .ok_or_else(|| {
                                io::Error::new(
                                    io::ErrorKind::InvalidInput,
                                    "--cellular-report-secs expects 1 through 86400",
                                )
                            })?,
                    );
                }
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
                ));
            }
        };
        if upload_interval.is_some() && collector.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "--cellular-report-secs requires a collector",
            ));
        }
        communications::validate(options.ble, apn.is_some(), options.tile_relay.is_some())?;
        options.cellular = apn.map(|apn| CellularConfig {
            apn,
            collector,
            interval: Duration::from_secs(upload_interval.unwrap_or(60)),
        });
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
    ReportReady,
    LinksUpdated,
    Touch { event: touch::Event, missed: bool },
}

pub(super) const USAGE: &str = "motion_tracker [--interval-ms 100..2000] [--frames N] [--stream-usb] [--ble] [--cellular-apn APN [--collector IPv4:PORT --collector-pin SHA256 [--cellular-report-secs SECS]]] [--map-relay IPv4:PORT --map-pin SHA256] [--map-zoom 15..18] or motion_tracker --battery";

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
    let mut screen = Screen::new(display::open().map_err(display_error)?).map_err(display_error)?;
    let mut dashboard = Dashboard::default();
    let zoom = options.zoom.unwrap_or_default();
    let mut history = History::new(zoom);
    let mut map = crate::map::View::default();
    let (events, inbox) = bounded(32);
    let notify = {
        let events = events.clone();
        move |update| {
            let event = match update {
                Update::Links => Event::LinksUpdated,
                Update::Report => Event::ReportReady,
            };
            // The latest values remain readable when the inbox is full.
            let _ = events.try_send(event);
        }
    };
    let local_reports = options.reports.usb_stream || options.reports.frames.is_some();
    let (telemetry, readings) =
        Telemetry::start(interval, local_reports, options.reports.usb_stream);
    let (mut snapshot, _) = readings.read().expect("initial tracker state");
    let mut local = Some(telemetry);
    let mut communications =
        (options.ble || options.cellular.is_some() || options.tile_relay.is_some())
            .then(|| {
                Communications::start(
                    options.ble,
                    options.cellular,
                    options.tile_relay,
                    &mut local,
                    notify.clone(),
                )
            })
            .transpose()?;
    let mut view = View::default();
    let mut missed = false;
    let mut touch = touch::open(move |event| {
        match events.try_send(Event::Touch { event, missed }) {
            Ok(()) => missed = false,
            Err(TrySendError::Full(_)) => missed = true,
            Err(TrySendError::Disconnected(_)) => return false,
        }
        true
    })
    .inspect_err(|error| log::warn!("motion-tracker touch: {error:?}"))
    .ok();

    let mut serial = options.reports.usb_stream.then(io::stdout);
    let mut rendered = 0_u32;
    let mut next_plot = Instant::now();
    let mut redraw = true;
    let mut links_changed = false;
    let result = loop {
        let now = Instant::now();
        if let Some(telemetry) = &mut local {
            let _ = telemetry.step(now);
        }
        let plot_due = now >= next_plot;
        if links_changed || plot_due {
            links_changed = false;
            if let Some(links) = &communications {
                let update = links.view(view);
                if update != view {
                    redraw = true;
                    #[cfg(feature = "ble-link")]
                    if let Some(key) = update.key {
                        if let Err(error) = dashboard.show_code(&key) {
                            break Err(display_error(error));
                        }
                    } else {
                        dashboard.hide_code();
                        if update.link == LinkStatus::Paired {
                            dashboard.open_menu();
                        }
                    }
                    view = update;
                }
            }
        }
        if plot_due {
            if let Some(links) = communications.as_mut().filter(|links| links.is_finished()) {
                break links
                    .stop()
                    .and(Err(io::Error::other("tracker links stopped")));
            }
            if touch.as_ref().is_some_and(touch::Session::is_finished) {
                let mut finished = touch.take().unwrap();
                let result = finished.join();
                log::warn!("motion-tracker touch stopped: {result:?}");
            }
        }
        let report = readings.read().and_then(|(current, report)| {
            snapshot = current;
            report
        });
        if let Some(snapshot) = report {
            if let Some(serial) = &mut serial {
                if let Err(error) = write_report(snapshot, &mut serial.lock()) {
                    break Err(error);
                }
            }
            if options.reports.frames.is_some() {
                rendered += 1;
                if options.reports.frames == Some(rendered) {
                    let _ = writeln!(
                        io::stderr().lock(),
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
        }
        if plot_due || redraw {
            if plot_due {
                history.record(&snapshot, now);
                next_plot = now + SAMPLE_INTERVAL;
            }
            let viewport = map.select(dashboard.map_position(view.link, &snapshot, zoom));
            if let Some(links) = &communications {
                if let Some(tile) = links.map(viewport.and_then(|viewport| map.wanted(viewport))) {
                    if let Some(viewport) = viewport {
                        map.accept(viewport, tile);
                    }
                }
            }
            if let Err(error) = render(view.link, || {
                screen.render(
                    &dashboard,
                    view.link,
                    &snapshot,
                    &history,
                    now,
                    viewport.map(|viewport| (viewport, &map)),
                )
            }) {
                break Err(error);
            }
            redraw = false;
            let finished = Instant::now();
            if finished >= next_plot {
                // Skip the missed paint and leave time to receive sensor and touch events.
                next_plot = finished + SAMPLE_INTERVAL;
            }
        }
        if plot_due {
            if let Err(error) = crate::health::progress(&mut timer) {
                break Err(error);
            }
        }
        let wake = local
            .as_ref()
            .map_or(next_plot, |telemetry| next_plot.min(telemetry.deadline()));
        match inbox.wait(Some(wake.saturating_duration_since(Instant::now()))) {
            Ok(Event::LinksUpdated) => {
                links_changed = true;
                redraw = true;
            }
            Ok(Event::ReportReady) => {}
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
                            None,
                            &mut local,
                            notify.clone(),
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
    };

    let stop_touch = touch.as_mut().map(touch::Session::request_stop);
    let stop_links = communications.as_mut().map(Communications::stop);
    let stop_sensors = local.as_mut().map(Telemetry::stop);
    let join_touch = touch.as_mut().map(touch::Session::join);
    for (operation, failed) in [
        ("stop touch", failed(stop_touch)),
        ("join touch", failed(join_touch)),
        ("stop communications", failed(stop_links)),
        ("stop sensor acquisition", failed(stop_sensors)),
    ] {
        if failed {
            log::error!("motion-tracker: {operation} failed");
            if result.is_ok() {
                return Err(io::Error::other(operation));
            }
        }
    }
    result
}

fn write_report(snapshot: tracker::Snapshot, writer: &mut impl Write) -> io::Result<()> {
    let frame = wire::frame(&snapshot)?;
    writer.write_all(frame.bytes())?;
    writer.flush()
}

fn failed<T>(result: Option<Result<(), T>>) -> bool {
    result.is_some_and(|result| result.is_err())
}

fn display_error(error: display::Error) -> io::Error {
    io::Error::other(format!("tracker display: {error:?}"))
}

fn render(link: LinkStatus, paint: impl FnOnce() -> Result<(), display::Error>) -> io::Result<()> {
    if link != LinkStatus::Connected {
        return paint().map_err(display_error);
    }
    // The display provider's synchronous SPI draws run at the product's
    // background priority, below acquisition and Bluetooth host workers.
    match execution::configure_current(execution::Priority::Background) {
        Ok(()) => {}
        Err(execution::Error::Unsupported) => return paint().map_err(display_error),
        Err(error) => {
            return Err(io::Error::other(format!(
                "tracker display priority: {error:?}"
            )));
        }
    }
    let painted = paint().map_err(display_error);
    let restored = execution::configure_current(execution::Priority::Normal)
        .map_err(|error| io::Error::other(format!("tracker restore priority: {error:?}")));
    painted.and(restored)
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
        let options = parse(&["--map-relay", "192.0.2.3:8444", "--map-pin", &pin]);
        if !communications::DIRECT_MAP_AVAILABLE {
            assert_eq!(options.err().unwrap().kind(), io::ErrorKind::Unsupported);
            return;
        }
        let options = options.unwrap();
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
        let options = launch("192.0.2.3:1234", &pin);
        if !communications::CELLULAR_AVAILABLE {
            assert_eq!(options.err().unwrap().kind(), io::ErrorKind::Unsupported);
            return;
        }
        let collector = options.unwrap().cellular.unwrap().collector.unwrap();
        assert_eq!(collector.pin, [0xab; 32]);
        assert!(parse(&["--cellular-apn", "example", "--cellular-report-secs", "30"]).is_err());
        let configured = |seconds| {
            parse(&[
                "--cellular-apn",
                "example",
                "--collector",
                "192.0.2.3:1234",
                "--collector-pin",
                &pin,
                "--cellular-report-secs",
                seconds,
            ])
        };
        assert!(configured("0").is_err());
        assert_eq!(
            configured("30").unwrap().cellular.unwrap().interval,
            Duration::from_secs(30)
        );
        assert!(
            parse(&["--cellular-apn", "example"])
                .unwrap()
                .cellular
                .unwrap()
                .collector
                .is_none()
        );
    }
}
