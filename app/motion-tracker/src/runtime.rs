use crate::{
    communications,
    health::{Component, Status, Supervisor},
    screen, tracker, ReportOptions,
};
use battery::Device as _;
use communications::{
    Capture, CellularConfig, Collector, Communications, Endpoint, Pairing, Telemetry, View, Zoom,
};
use messaging::bounded;
use navigation::observations::{fixed_readers, Observation, ObservationService, Publisher};
use screen::{Action, Screen};
use std::io::{self, Write};
use std::sync::{
    mpsc::{RecvTimeoutError, TrySendError},
    Arc,
};
use std::time::{Duration, Instant};
use touch::Session as _;
use tracker::{report_fault, Snapshot, FIX_FRESH, IMU_FRESH};

struct Options {
    reports: ReportOptions,
    ble: bool,
    battery: bool,
    cellular: Option<CellularConfig>,
    tile_relay: Option<Endpoint>,
    zoom: Option<Zoom>,
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
        let mut report_interval = None;
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
                    let seconds = value(&mut args)?
                        .parse()
                        .ok()
                        .filter(|seconds| (1..=86_400).contains(seconds))
                        .ok_or_else(|| {
                            io::Error::new(
                                io::ErrorKind::InvalidInput,
                                "--cellular-report-secs expects 1 through 86400",
                            )
                        })?;
                    report_interval = Some(Duration::from_secs(seconds));
                }
                "--map-relay" => tile_relay = Some(value(&mut args)?),
                "--map-pin" => tile_pin = Some(value(&mut args)?),
                "--map-zoom" => {
                    options.zoom = Some(
                        value(&mut args)?
                            .parse()
                            .ok()
                            .and_then(Zoom::new)
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
            (None, None) if report_interval.is_none() => None,
            (Some(address), Some(pin)) if apn.is_some() => Some(Collector {
                endpoint: Endpoint::parse(&address, &pin)?,
                interval: report_interval.unwrap_or(Duration::from_secs(60)),
            }),
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "--collector and --collector-pin require each other and --cellular-apn. --cellular-report-secs requires a collector",
                ));
            }
        };
        options.tile_relay = match (tile_relay, tile_pin) {
            (None, None) => None,
            (Some(address), Some(pin))
                if communications::DIRECT_RELAY_AVAILABLE && apn.is_none() =>
            {
                Some(Endpoint::parse(&address, &pin)?)
            }
            (Some(_), Some(_)) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "direct map relay is unavailable with cellular. Use BLE for maps.",
                ));
            }
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

pub(super) const USAGE: &str = "motion_tracker [--interval-ms 100..2000] [--frames N] [--stream-usb] [--ble] [--cellular-apn APN [--collector IPv4:PORT --collector-pin SHA256 [--cellular-report-secs 1..86400]]] [--map-relay IPv4:PORT --map-pin SHA256] [--map-zoom 15..18] or motion_tracker --battery";

pub(super) fn run(args: impl Iterator<Item = String>) -> io::Result<()> {
    let options = Options::parse(args)?;
    if options.battery {
        return report_battery();
    }
    crate::health::run(|timer| track(options, timer))
}

fn sensor_events(
    mut output: Publisher<2>,
    capture: Arc<Capture>,
) -> impl FnMut(Observation) -> Option<Event> {
    move |event| {
        // Publish current availability before capture wakes communications. A
        // higher-priority reader must not wake to the preceding live solution.
        let published = output.record(&event);
        if let Some(status) = event.no_fix() {
            capture.gnss_unavailable(status);
        }
        match published {
            Ok(sample) => {
                if let Some(sample) = sample {
                    capture.record(sample);
                }
                None
            }
            Err(fault) => {
                capture.source_failed(fault);
                Some(Event::SensorChanged)
            }
        }
    }
}

fn track(options: Options, supervisor: &mut Supervisor<'_>) -> io::Result<()> {
    let interval = crate::preferences(options.reports.interval)?.duration();
    let status = Arc::new(Status::default());
    let mut screen = match display::open()
        .and_then(|display| Screen::new(display, options.zoom.unwrap_or_default()))
    {
        Ok(screen) => Some(screen),
        Err(display::Error::Unsupported) => None,
        Err(error) => {
            status.fail(Component::Display);
            log::warn!("motion-tracker display disabled: {error:?}");
            None
        }
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
    let mut sensors = ObservationService::new(IMU_FRESH, Some(FIX_FRESH));
    let ([primary_output, secondary_output], gnss_output, [mut dashboard, links]) =
        fixed_readers(IMU_FRESH, FIX_FRESH);
    let telemetry = Telemetry::new(links, capture.clone(), interval, status.clone());
    supervisor.owners_started();
    let mut communications =
        (options.ble || options.cellular.is_some() || options.tile_relay.is_some())
            .then(|| {
                Communications::start(
                    options.ble,
                    options.cellular.clone(),
                    options.tile_relay,
                    telemetry.clone(),
                    notify_links.clone(),
                )
            })
            .and_then(|result| match result {
                Ok(links) => Some(links),
                Err(error) => {
                    status.fail(Component::Communications);
                    log::warn!("motion-tracker communications unavailable: {error}");
                    None
                }
            });
    let mut view = View::default();
    let mut reported_imu_failures = [None; 2];
    let mut required = Ok(());
    for result in [
        sensors.start_imu_with(
            0,
            primary,
            events.clone(),
            sensor_events(primary_output, capture.clone()),
        ),
        sensors.start_gnss_with(
            composition::gnss::primary_gnss(),
            events.clone(),
            sensor_events(gnss_output, capture.clone()),
        ),
    ] {
        if let Err(fault) = result {
            dashboard.fail(fault);
            capture.source_failed(fault);
            report_fault(fault);
            required = Err(io::Error::other(format!("open required sensor: {fault:?}")));
        }
    }
    if required.is_ok() && secondary_present {
        if let Err(fault) = sensors.start_imu_with(
            1,
            secondary,
            events.clone(),
            sensor_events(secondary_output, capture.clone()),
        ) {
            dashboard.fail(fault);
            capture.source_failed(fault);
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
                status.fail(Component::Touch);
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
                status.fail(Component::Display);
                log::warn!("motion-tracker display disabled; priority setup: {error:?}");
                screen = None;
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
                        let updated = screen.as_mut().map(|screen| screen.update_link(update));
                        if let Some(Err(error)) = updated {
                            status.fail(Component::Display);
                            log::warn!("motion-tracker display disabled: {error:?}");
                            screen = None;
                        }
                        redraw = true;
                    }
                    view = update;
                }
            }
            if report_due {
                if communications
                    .as_ref()
                    .is_some_and(Communications::is_finished)
                {
                    let mut finished = communications.take().unwrap();
                    status.fail(Component::Communications);
                    log::warn!(
                        "motion-tracker communications retired: {:?}",
                        finished.join()
                    );
                }
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
                sensors.reap_finished(|fault| {
                    dashboard.fail(fault);
                    capture.source_failed(fault);
                    report_fault(fault);
                });
                for (index, (current, reported)) in sensors
                    .imu_failures()
                    .into_iter()
                    .zip(&mut reported_imu_failures)
                    .enumerate()
                {
                    if current != *reported {
                        if let Some(failure) = current {
                            log::warn!(
                                "motion-tracker IMU {} terminal evidence: {failure:?}",
                                index + 1
                            );
                        }
                        *reported = current;
                    }
                }
                if touch.as_ref().is_some_and(touch::Session::is_finished) {
                    let mut finished = touch.take().unwrap();
                    let result = finished.join();
                    status.fail(Component::Touch);
                    log::warn!("motion-tracker touch stopped: {result:?}");
                }
            }
            if report_due {
                let mut snapshot = Snapshot::from(dashboard.snapshot(now));
                snapshot.clock = capture.clock();
                snapshot.output_faults = status.snapshot();
                let written = serial
                    .as_mut()
                    .map(|serial| communications::write_usb(&mut serial.lock(), &snapshot));
                if let Some(Err(error)) = written {
                    status.fail(Component::Usb);
                    log::warn!("motion-tracker USB stream disabled: {error}");
                    serial = None;
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
                        use tracker::Reading::{Fault, Live, Received};
                        break if !matches!(snapshot.imu[0], Live(_) | Received(_))
                            || (secondary_present
                                && !matches!(snapshot.imu[1], Live(_) | Received(_)))
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
            if let Some(active) = screen.as_mut().filter(|_| plot_due || redraw) {
                let mut snapshot = Snapshot::from(dashboard.snapshot(now));
                snapshot.output_faults = status.snapshot();
                let result = active.draw(
                    view.link,
                    &snapshot,
                    now,
                    || dashboard.satellites(now),
                    |wanted| communications.as_ref().and_then(|links| links.map(wanted)),
                );
                if let Err(error) = result {
                    status.fail(Component::Display);
                    log::warn!("motion-tracker display disabled: {error:?}");
                    screen = None;
                }
                redraw = false;
            }
            if report_due {
                if let Err(error) = supervisor.progress(Instant::now()) {
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
                        Some(Action::Pair) => {
                            if communications
                                .as_ref()
                                .is_some_and(Communications::is_finished)
                            {
                                let mut retired = communications.take().unwrap();
                                let _ = retired.join();
                                status.fail(Component::Communications);
                            }
                            match &communications {
                                Some(links) => links.pair(Pairing::Start),
                                None => match Communications::start(
                                    true,
                                    options.cellular.clone(),
                                    options.tile_relay,
                                    telemetry.clone(),
                                    notify_links.clone(),
                                ) {
                                    Ok(links) => communications = Some(links),
                                    Err(error) => {
                                        status.fail(Component::Communications);
                                        log::warn!("motion-tracker pairing: {error}");
                                    }
                                },
                            }
                        }
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

    // Cancellation is concurrent. Only join owners that have finished, leaving
    // the supervisor responsive during modem teardown or a failed wake.
    let deadline = supervisor.begin_shutdown(Instant::now());
    let mut cleanup_failed = false;
    sensors.request_stop(|fault| {
        cleanup_failed = true;
        report_fault(fault);
    });
    if let Some(touch) = &mut touch {
        if let Err(error) = touch.request_stop() {
            cleanup_failed = true;
            log::error!("motion-tracker stop touch: {error:?}");
        }
    }
    if let Some(links) = &mut communications {
        links.request_stop();
    }
    let mut attempts = 1_u8;
    let mut retry = Instant::now() + Duration::from_millis(100);
    loop {
        sensors.reap_finished(|fault| {
            cleanup_failed = true;
            report_fault(fault);
        });
        if touch.as_ref().is_some_and(touch::Session::is_finished) {
            let mut finished = touch.take().unwrap();
            if let Err(error) = finished.join() {
                cleanup_failed = true;
                log::error!("motion-tracker join touch: {error:?}");
            }
        }
        if communications
            .as_ref()
            .is_some_and(Communications::is_finished)
        {
            let mut finished = communications.take().unwrap();
            if let Err(error) = finished.join() {
                cleanup_failed = true;
                log::error!("motion-tracker join communications: {error}");
            }
        }
        if sensors.is_quiescent() && touch.is_none() && communications.is_none() {
            for (index, failure) in sensors.imu_failures().into_iter().enumerate() {
                if let Some(failure) = failure.filter(|failure| failure.cleanup.is_some()) {
                    cleanup_failed = true;
                    log::error!(
                        "motion-tracker IMU {} cleanup evidence: {failure:?}",
                        index + 1
                    );
                }
            }
            supervisor.owners_retired();
            break;
        }
        let now = Instant::now();
        if let Err(error) = supervisor.progress(now) {
            // Keep retained owners until this deadline. Their nonjoining Drop
            // requests cancellation; the armed watchdog handles a stranded owner.
            return result.and(Err(error));
        }
        if attempts < 3 && now >= retry {
            attempts += 1;
            sensors.request_stop(|fault| {
                cleanup_failed = true;
                report_fault(fault);
            });
            if let Some(touch) = &mut touch {
                if let Err(error) = touch.request_stop() {
                    cleanup_failed = true;
                    log::warn!("motion-tracker retry stop touch: {error:?}");
                }
            }
            retry = now + Duration::from_millis(100 * u64::from(attempts));
        }
        let wake = deadline.min(now + Duration::from_millis(50));
        let _ = inbox.wait(Some(wake.saturating_duration_since(Instant::now())));
    }
    if cleanup_failed {
        result.and(Err(io::Error::other("tracker cleanup reported a fault")))
    } else {
        result
    }
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
        let args = ["--map-relay", "192.0.2.3:8444", "--map-pin", &pin];
        assert!(parse(&[&args[..], &["--cellular-apn", "example"]].concat()).is_err());
        if cellular::LINK_AVAILABLE {
            assert!(parse(&args).is_err());
            return;
        }
        let options = parse(&args).unwrap();
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
        assert!(parse(&["--cellular-apn", "example", "--cellular-report-secs", "5"]).is_err());
        let args = [
            "--cellular-apn",
            "example",
            "--collector",
            "192.0.2.3:1234",
            "--collector-pin",
            &pin,
        ];
        let timed = |secs| parse(&[&args[..], &["--cellular-report-secs", secs]].concat());
        assert!(timed("0").is_err());
        assert!(timed("86401").is_err());
        assert_eq!(
            timed("5")
                .unwrap()
                .cellular
                .unwrap()
                .collector
                .unwrap()
                .interval,
            Duration::from_secs(5)
        );

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
        assert_eq!(collector.endpoint.pin, [0xab; 32]);
        assert_eq!(collector.interval, Duration::from_secs(60));
        assert!(parse(&["--cellular-apn", "example"])
            .unwrap()
            .cellular
            .unwrap()
            .collector
            .is_none());
    }
}
