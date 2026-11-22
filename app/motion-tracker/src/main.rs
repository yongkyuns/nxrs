//! Dual-IMU and GNSS dashboard with a bounded, transport-independent snapshot stream.
#![forbid(unsafe_code)]

mod charts;
mod preferences;
mod radio;
mod screen;
mod text;
mod tracker;
mod wire;

use battery::Device as _;
use ble::peripheral::Listener;
use charts::{History, SAMPLE_INTERVAL};
use gnss::{Device as _, Session as _};
use imu::Device as _;
use messaging::bounded;
use preferences::Interval;
use screen::{Action, Dashboard, LinkStatus, Screen};
use std::cell::OnceCell;
use std::io::{self, Write};
use std::sync::mpsc::{RecvTimeoutError, TrySendError};
use std::time::{Duration, Instant};
use touch::Session as _;
use tracker::{sink, Input, Tracker};

struct Options {
    interval: Option<Interval>,
    frames: Option<u32>,
    usb_stream: bool,
    ble: bool,
    battery: bool,
    cellular_apn: Option<String>,
}

impl Options {
    fn parse(mut args: impl Iterator<Item = String>) -> io::Result<Self> {
        let mut options = Self {
            interval: None,
            frames: None,
            usb_stream: false,
            ble: false,
            battery: false,
            cellular_apn: None,
        };
        while let Some(arg) = args.next() {
            let value = |args: &mut dyn Iterator<Item = String>| {
                args.next().ok_or(io::ErrorKind::InvalidInput)
            };
            match arg.as_str() {
                "--interval-ms" => {
                    options.interval = Some(
                        value(&mut args)?
                            .parse()
                            .ok()
                            .and_then(Interval::new)
                            .ok_or(io::ErrorKind::InvalidInput)?,
                    );
                }
                "--frames" => {
                    options.frames = Some(
                        value(&mut args)?
                            .parse()
                            .ok()
                            .filter(|count| *count > 0)
                            .ok_or(io::ErrorKind::InvalidInput)?,
                    );
                }
                "--stream-usb" => options.usb_stream = true,
                "--ble" => options.ble = true,
                "--cellular-apn" => options.cellular_apn = Some(value(&mut args)?),
                "--battery" => options.battery = true,
                _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, arg)),
            }
        }
        if options.battery
            && (options.interval.is_some()
                || options.frames.is_some()
                || options.usb_stream
                || options.ble
                || options.cellular_apn.is_some())
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "--battery must be used on its own",
            ));
        }
        Ok(options)
    }
}

fn preferences(requested: Option<Interval>) -> io::Result<Interval> {
    let result = settings::open().and_then(|mut store| match requested {
        Some(interval) => {
            // Only the explicit settings command writes flash.
            if Interval::load(&mut store) != Ok(interval) {
                interval.save(&mut store)?;
            }
            Ok(interval)
        }
        None => Interval::load(&mut store),
    });
    match result {
        Ok(interval) => Ok(interval),
        Err(error) if requested.is_none() => {
            eprintln!("motion-tracker settings: {error:?}, using defaults");
            Ok(Interval::DEFAULT)
        }
        Err(error) => Err(io::Error::other(format!(
            "save tracker settings: {error:?}"
        ))),
    }
}

fn imu_status(session: &mut Option<impl imu::Session>, index: usize, tracker: &mut Tracker) {
    if session.as_ref().is_some_and(imu::Session::is_finished) {
        let mut finished = session.take().unwrap();
        tracker.imu_failed(index, finished.join().err().unwrap_or(imu::DeviceError::Io));
    }
}

enum Event {
    Sensor(tracker::Observation),
    Touch { event: touch::Event, missed: bool },
}

fn link_status(output: Option<&radio::Output<'_, impl Listener>>, failed: bool) -> LinkStatus {
    match output {
        Some(output) if output.live() => LinkStatus::Connected,
        Some(output) if output.paired() => LinkStatus::Paired,
        Some(_) => LinkStatus::Pairing,
        None if failed => LinkStatus::Unavailable,
        None => LinkStatus::Idle,
    }
}

fn service_radio(output: Option<&mut radio::Output<'_, impl Listener>>, deadline: &mut Instant) {
    let now = Instant::now();
    if let Some(output) = output.filter(|_| now >= *deadline) {
        output.service();
        *deadline = now + Duration::from_millis(15);
    }
}

fn run(options: Options) -> io::Result<()> {
    if options.battery {
        return report_battery();
    }
    let interval = preferences(options.interval)?.duration();
    let mut cellular = options
        .cellular_apn
        .as_deref()
        .map(cellular::connect)
        .transpose()
        .map_err(io::Error::other)?;
    let mut reported_cellular = None;
    let mut screen = Screen::new(display::open().map_err(display_error)?).map_err(display_error)?;
    let mut dashboard = Dashboard::default();
    let mut history = History::new();
    let identity = OnceCell::new();
    let mut radio = options.ble.then(|| radio::pair(&identity)).transpose()?;
    let mut pairing_failed = false;
    if let Some(output) = &radio {
        dashboard.show_code(output.key()).map_err(display_error)?;
    }
    let mut tracker = Tracker::new();
    let (events, inbox) = bounded(32);
    let mut primary = composition::imu::primary_imu()
        .open_default(sink(
            events.clone(),
            |event| Input::Imu(0, event),
            Event::Sensor,
        ))
        .inspect_err(|error| tracker.imu_failed(0, *error))
        .ok();
    let mut secondary = composition::imu::secondary()
        .open_default(sink(
            events.clone(),
            |event| Input::Imu(1, event),
            Event::Sensor,
        ))
        .inspect_err(|error| tracker.imu_failed(1, *error))
        .ok();
    let mut receiver = composition::gnss::primary_gnss()
        .open(sink(events.clone(), Input::Gnss, Event::Sensor))
        .inspect_err(|error| tracker.gnss_failed(*error))
        .ok();
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

    let mut serial = options.usb_stream.then(io::stdout);
    let mut reported_drops = [0; 3];
    let mut rendered = 0_u32;
    let mut next = Instant::now();
    let mut next_plot = next;
    let mut next_radio = next;
    let mut shown_link = None;
    let mut redraw = true;
    let result = loop {
        let now = Instant::now();
        if radio.as_ref().is_some_and(|output| output.expired(now)) {
            radio = None;
        }
        service_radio(radio.as_mut(), &mut next_radio);
        let link = link_status(radio.as_ref(), pairing_failed);
        if shown_link != Some(link) {
            redraw = true;
            if link != LinkStatus::Pairing {
                dashboard.hide_code();
            }
        }
        let report_due = now >= next;
        let plot_due = now >= next_plot;
        if report_due {
            if serial.is_some() {
                let drops = [
                    primary.as_ref().map(imu::Session::dropped),
                    secondary.as_ref().map(imu::Session::dropped),
                    receiver.as_ref().map(gnss::Session::dropped),
                ];
                for ((name, count), reported) in ["IMU 1", "IMU 2", "GNSS"]
                    .into_iter()
                    .zip(drops)
                    .zip(&mut reported_drops)
                {
                    if let Some(count) = count.filter(|count| *count > *reported) {
                        eprintln!("motion-tracker {name} queue drops: {count}");
                        *reported = count;
                    }
                }
            }
            if let Some(status) = cellular.as_ref().map(cellular::Session::status) {
                if reported_cellular != Some(status) {
                    eprintln!("motion-tracker cellular {status:?}");
                    reported_cellular = Some(status);
                }
            }
            // A full inbox can reject the terminal event; the join still reports it.
            imu_status(&mut primary, 0, &mut tracker);
            imu_status(&mut secondary, 1, &mut tracker);
            if receiver.as_ref().is_some_and(gnss::Session::is_finished) {
                let mut finished = receiver.take().unwrap();
                tracker.gnss_failed(finished.join().err().unwrap_or(gnss::DeviceError::Io));
            }
            if touch.as_ref().is_some_and(touch::Session::is_finished) {
                let mut finished = touch.take().unwrap();
                eprintln!("motion-tracker touch stopped: {:?}", finished.join());
            }
        }
        if report_due {
            let snapshot = tracker.snapshot(now);
            if let Some(output) = &mut radio {
                output.offer(&snapshot, now);
            }
            if let Some(serial) = &mut serial {
                let mut writer = serial.lock();
                let written = wire::frame(&snapshot)
                    .and_then(|frame| writer.write_all(frame.bytes()))
                    .and_then(|()| writer.flush());
                if let Err(error) = written {
                    break Err(error);
                }
            }
            if options.frames.is_some() {
                rendered += 1;
                if options.frames == Some(rendered) {
                    eprintln!(
                        "motion-tracker: {rendered} frames, interval={} ms, IMU 1 {}, IMU 2 {}, GNSS {}",
                        interval.as_millis(), snapshot.imu[0].status(), snapshot.imu[1].status(),
                        snapshot.gnss.status(),
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
            let snapshot = tracker.snapshot(now);
            if plot_due {
                history.record(&snapshot, now);
                next_plot = now + SAMPLE_INTERVAL;
            }
            if let Err(error) = screen.render(&dashboard, link, &snapshot, &history, now, || {
                service_radio(radio.as_mut(), &mut next_radio);
            }) {
                break Err(display_error(error));
            }
            redraw = false;
            shown_link = Some(link);
            let finished = Instant::now();
            if finished >= next_plot {
                // Skip the missed paint and leave time to receive sensor and touch events.
                next_plot = finished + SAMPLE_INTERVAL;
            }
        }
        let wake = next.min(next_plot);
        let wake = if radio.is_some() {
            wake.min(next_radio)
        } else {
            wake
        };
        match inbox.wait(Some(wake.saturating_duration_since(Instant::now()))) {
            Ok(Event::Sensor(event)) => tracker.observe(event),
            Ok(Event::Touch { event, missed }) => {
                if missed {
                    dashboard.cancel_touch();
                }
                let link = link_status(radio.as_ref(), pairing_failed);
                match screen.input(&mut dashboard, link, event) {
                    Some(Action::Pair) => {
                        match radio::pair(&identity) {
                            Ok(output) => {
                                if let Err(error) = dashboard.show_code(output.key()) {
                                    break Err(display_error(error));
                                }
                                radio = Some(output);
                                next_radio = Instant::now();
                                pairing_failed = false;
                            }
                            Err(error) => {
                                eprintln!("motion-tracker pairing: {error}");
                                pairing_failed = true;
                            }
                        }
                        redraw = true;
                    }
                    Some(Action::CancelPairing) => {
                        radio = None;
                        dashboard.hide_code();
                        redraw = true;
                    }
                    Some(Action::Redraw) => redraw = true,
                    None => {}
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            // With every source faulted, retain diagnostics on screen and keep
            // the requested display cadence without spinning on a closed inbox.
            Err(RecvTimeoutError::Disconnected) => {
                std::thread::sleep(wake.saturating_duration_since(Instant::now()));
            }
        }
    };

    // Ask all workers to stop before waiting for any one join.
    let stop_a = primary.as_mut().map(imu::Session::request_stop);
    let stop_b = secondary.as_mut().map(imu::Session::request_stop);
    let stop_gnss = receiver.as_mut().map(gnss::Session::request_stop);
    let stop_touch = touch.as_mut().map(touch::Session::request_stop);
    let join_a = primary.as_mut().map(imu::Session::join);
    let join_b = secondary.as_mut().map(imu::Session::join);
    let join_gnss = receiver.as_mut().map(gnss::Session::join);
    let join_touch = touch.as_mut().map(touch::Session::join);
    let stop_cellular = cellular.as_mut().map(cellular::Session::stop);
    for (operation, failed) in [
        ("stop IMU 1", failed(stop_a)),
        ("stop IMU 2", failed(stop_b)),
        ("stop GNSS", failed(stop_gnss)),
        ("stop touch", failed(stop_touch)),
        ("join IMU 1", failed(join_a)),
        ("join IMU 2", failed(join_b)),
        ("join GNSS", failed(join_gnss)),
        ("join touch", failed(join_touch)),
        ("stop cellular", failed(stop_cellular)),
    ] {
        if failed {
            eprintln!("motion-tracker: {operation} failed");
            if result.is_ok() {
                return Err(io::Error::other(operation));
            }
        }
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

fn main() -> std::process::ExitCode {
    match Options::parse(std::env::args().skip(1)).and_then(run) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!(
                "motion_tracker [--interval-ms 100..2000] [--frames N] [--stream-usb] [--ble] [--cellular-apn APN] or motion_tracker --battery: {error}"
            );
            std::process::ExitCode::FAILURE
        }
    }
}
