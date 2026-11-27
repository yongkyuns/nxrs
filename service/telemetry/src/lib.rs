//! Navigation telemetry, bounded capture and authenticated delivery.
//!
//! One owner steps BLE, collector TLS and map transfer under one identity lease.
//! Applications supply product identity, endpoints and resource budgets. Wire
//! encoders and synchronous publication can be used without starting that owner.
#![forbid(unsafe_code)]

mod clock;
pub mod export;
pub mod publication;
mod radio;
mod snapshot;
mod tiles;
mod uplink;
pub mod wire;

pub use clock::ClockAnchor;
pub use export::{Capture, CaptureBudget, CaptureConfig, CapturePublisher, CaptureView};
pub use snapshot::{reading_status, Reading, Snapshot};
pub use uplink::{Collector, Config as CellularConfig, Endpoint};
pub use wire::write_usb;

use maps::{Key, Tile};
use navigation::observations::ObservationReader;
use std::cell::OnceCell;
use std::io;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tiles::Response;

const ACTIVE_POLL: Duration = Duration::from_millis(15);

/// Longest supported summary interval. A bounded interval also keeps scheduling
/// arithmetic representable throughout the lifetime of a running owner.
pub const MAX_REPORT_INTERVAL: Duration = Duration::from_secs(86_400);

/// The legacy polling and notification-capable BLE byte-stream services.
/// Product UUIDs belong to application composition, not the delivery owner.
#[derive(Clone, Copy)]
pub struct BleServices {
    pub pairing: ble::peripheral::Service,
    pub telemetry: ble::peripheral::Service,
}

/// Human-readable labels for diagnostics and the existing execution owners.
#[derive(Clone, Copy)]
pub struct Names {
    pub product: &'static str,
    pub worker: &'static str,
    pub collector_worker: &'static str,
    pub tile_worker: &'static str,
}

impl Default for Names {
    fn default() -> Self {
        Self {
            product: "telemetry",
            worker: "telemetry-links",
            collector_worker: "telemetry-dial",
            tile_worker: "telemetry-tile",
        }
    }
}

/// Connections selected by the application. Supplying a tile relay admits its
/// use; the application decides whether the underlying network may carry maps.
pub struct Config {
    pub ble: BleServices,
    pub pair_on_start: bool,
    pub cellular: Option<CellularConfig>,
    pub tile_relay: Option<Endpoint>,
    pub names: Names,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LinkStatus {
    Idle,
    Pairing,
    Paired,
    Connected,
    Unavailable,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub struct View {
    pub link: LinkStatus,
    pub key: Option<[u8; 32]>,
}

impl Default for View {
    fn default() -> Self {
        Self {
            link: LinkStatus::Idle,
            key: None,
        }
    }
}

pub enum Pairing {
    Start,
    Cancel,
}

#[derive(Default)]
struct Shared {
    stopping: bool,
    pairing: Option<Pairing>,
    view: View,
    tile_request: Option<Key>,
    tile: Option<Tile>,
    // Ownership handoff only: populated before spawn and after the worker exits.
    // The running worker reads its endpoint outside this control-state mutex.
    telemetry: Option<Telemetry>,
}

pub struct Telemetry {
    pub readings: ObservationReader<2>,
    pub capture: Capture,
    pub interval: Duration,
}

impl Telemetry {
    fn snapshot_from(
        readings: &mut ObservationReader<2>,
        capture: &export::CaptureView,
        now: Instant,
    ) -> Snapshot {
        let mut snapshot = Snapshot::from(readings.snapshot(now));
        snapshot.clock = capture.clock();
        snapshot
    }

    #[cfg(test)]
    fn snapshot(&mut self, now: Instant) -> Snapshot {
        Self::snapshot_from(&mut self.readings, &self.capture.view(), now)
    }
}

struct TelemetryOwner<'a> {
    shared: &'a Mutex<Shared>,
    telemetry: Option<Telemetry>,
}

impl<'a> TelemetryOwner<'a> {
    fn take(shared: &'a Mutex<Shared>) -> Self {
        let telemetry = shared.lock().unwrap().telemetry.take();
        assert!(
            telemetry.is_some(),
            "communications reader already attached"
        );
        Self { shared, telemetry }
    }
}

impl Drop for TelemetryOwner<'_> {
    fn drop(&mut self) {
        // Even initialization failure or unwind closes admission before the
        // exclusive endpoint is handed back. A paused publisher may retire
        // later; the next joined owner continues bounded cleanup.
        if let Some(telemetry) = self.telemetry.as_mut() {
            telemetry.capture.finish();
        }
        self.shared
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .telemetry = self.telemetry.take();
    }
}

pub struct Communications {
    shared: Arc<Mutex<Shared>>,
    task: Option<JoinHandle<io::Result<()>>>,
}

impl Communications {
    pub fn start(
        config: Config,
        telemetry: &mut Option<Telemetry>,
        mut notify: impl FnMut() + Send + 'static,
    ) -> io::Result<Self> {
        if let Some(telemetry) = telemetry.as_ref() {
            validate_interval(telemetry.interval, "telemetry report interval")?;
        }
        if let Some(collector) = config.cellular.as_ref().and_then(|config| config.collector) {
            validate_interval(collector.interval, "collector report interval")?;
        }
        Self::start_owner(telemetry, move |incoming| {
            thread::Builder::new()
                .name(config.names.worker.into())
                .stack_size(23 * 1024)
                .spawn(move || {
                    let mut owner = TelemetryOwner::take(&incoming);
                    let product = config.names.product;
                    let result = run(
                        &incoming,
                        config,
                        owner.telemetry.as_mut().unwrap(),
                        &mut notify,
                    );
                    if let Err(error) = &result {
                        incoming.lock().unwrap().view = unavailable();
                        notify();
                        log::error!("{product} communications stopped: {error}");
                    }
                    result
                })
        })
    }

    fn start_owner(
        telemetry: &mut Option<Telemetry>,
        spawn: impl FnOnce(Arc<Mutex<Shared>>) -> io::Result<JoinHandle<io::Result<()>>>,
    ) -> io::Result<Self> {
        let owned = telemetry.take().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "communications reader is in use",
            )
        })?;
        let shared = Arc::new(Mutex::new(Shared {
            telemetry: Some(owned),
            ..Shared::default()
        }));
        match spawn(shared.clone()) {
            Ok(task) => Ok(Self {
                shared,
                task: Some(task),
            }),
            Err(error) => {
                *telemetry = shared.lock().unwrap().telemetry.take();
                Err(error)
            }
        }
    }

    pub fn pair(&self, command: Pairing) {
        let Some(task) = self.task.as_ref().filter(|task| !task.is_finished()) else {
            return;
        };
        self.shared.lock().unwrap().pairing = Some(command);
        task.thread().unpark();
    }

    pub fn view(&self, previous: View) -> View {
        if self.task.as_ref().is_none_or(JoinHandle::is_finished) {
            return unavailable();
        }
        self.shared
            .try_lock()
            .map_or(previous, |shared| shared.view)
    }

    pub fn map(&self, wanted: Option<Key>) -> Option<Tile> {
        let task = self.task.as_ref().filter(|task| !task.is_finished())?;
        let Ok(mut shared) = self.shared.try_lock() else {
            return None;
        };
        let tile = shared.tile.take();
        let wanted = wanted.filter(|key| tile.as_ref().is_none_or(|tile| tile.key != *key));
        if shared.tile_request != wanted {
            shared.tile_request = wanted;
            task.thread().unpark();
        }
        tile
    }

    pub fn stop(&mut self) -> io::Result<()> {
        if let Some(task) = self.task.take() {
            self.shared
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .stopping = true;
            task.thread().unpark();
            return task
                .join()
                .map_err(|_| io::Error::other("telemetry links task panicked"))?;
        }
        Ok(())
    }

    /// Recover the fixed reader only after joining its previous owner. A later
    /// start may reuse it without adding a subscriber or losing unread values.
    /// Capture can still be retiring a paused acquisition callback: the joined
    /// owner returns promptly and the next owner drains before opening an epoch.
    /// Full application shutdown joins acquisition before retiring this endpoint.
    pub fn take_telemetry(&mut self) -> Option<Telemetry> {
        if self.task.is_some() {
            return None;
        }
        self.shared
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .telemetry
            .take()
    }
}

fn validate_interval(interval: Duration, name: &str) -> io::Result<()> {
    if interval.is_zero()
        || interval > MAX_REPORT_INTERVAL
        || Instant::now().checked_add(interval).is_none()
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{name} must be greater than zero and at most one day"),
        ));
    }
    Ok(())
}

fn unavailable() -> View {
    View {
        link: LinkStatus::Unavailable,
        key: None,
    }
}

impl Drop for Communications {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn tls_identity(slot: &OnceCell<tls::Identity>) -> io::Result<&tls::Identity> {
    if slot.get().is_none() {
        let identity = tls::open().map_err(|error| io::Error::other(format!("TLS: {error:?}")))?;
        let _ = slot.set(identity);
    }
    Ok(slot.get().unwrap())
}

fn run(
    shared: &Mutex<Shared>,
    mut config: Config,
    telemetry: &mut Telemetry,
    notify: &mut impl FnMut(),
) -> io::Result<()> {
    // Pairing from the display can spawn this worker after the display has
    // taken its fixed background priority.
    match execution::configure_current(execution::Priority::Normal) {
        Ok(()) | Err(execution::Error::Unsupported) => {}
        Err(error) => {
            return Err(io::Error::other(format!(
                "telemetry links priority: {error:?}"
            )))
        }
    }
    telemetry.capture.register_owner();
    let collector = config.cellular.as_ref().and_then(|config| config.collector);
    let mut modem = config
        .cellular
        .take()
        .map(|cellular| uplink::Modem::start(cellular, config.names))
        .transpose()?;
    serve(
        shared,
        &config,
        modem.as_ref().map(|modem| (modem, collector)),
        telemetry,
        notify,
    );
    // Both transports and their shared TLS identity have left scope.
    modem.as_mut().map_or(Ok(()), uplink::Modem::stop)
}

fn serve(
    shared: &Mutex<Shared>,
    config: &Config,
    modem: Option<(&uplink::Modem, Option<uplink::Collector>)>,
    telemetry: &mut Telemetry,
    notify: &mut impl FnMut(),
) {
    let identity = OnceCell::new();
    let capture_view = telemetry.capture.view();
    let capture = &mut telemetry.capture;
    let interval = telemetry.interval;
    let readings = &mut telemetry.readings;
    let mut snapshot = |now| Telemetry::snapshot_from(readings, &capture_view, now);
    let mut radio: Option<radio::Output<'_, _>> = None;
    let mut tiles = tiles::Tiles::new(config.tile_relay, Instant::now(), config.names);
    let mut next_radio = Instant::now();
    let mut upload = None;
    let mut retry = uplink::Retry::new();
    let mut retry_pending = false;
    let mut link_generation = 0;
    let mut pairing_failed = false;
    let mut command = config.pair_on_start.then_some(Pairing::Start);
    let mut next_report = Instant::now();
    let mut cellular = modem.and_then(|(modem, collector)| {
        collector.map(|collector| {
            (
                collector.endpoint,
                modem,
                uplink::Reports::new(collector.interval, Instant::now()),
            )
        })
    });
    let mut cellular_status = None;

    loop {
        let (pair, tile) = {
            let mut inbox = shared.lock().unwrap();
            if inbox.stopping {
                break;
            }
            let tile = inbox
                .tile_request
                .filter(|key| inbox.tile.as_ref().is_none_or(|tile| tile.key != *key));
            (inbox.pairing.take().or(command.take()), tile)
        };
        capture.retire();
        let now = Instant::now();
        if let Some((modem, _)) = modem {
            let status = modem.status();
            if cellular_status != Some(status) {
                log::info!("{} cellular {status:?}", config.names.product);
                cellular_status = Some(status);
            }
        }
        if radio.as_ref().is_some_and(|output| output.expired(now)) {
            capture.finish();
            radio = None;
        }
        if let Some(pair) = pair {
            pairing_failed = false;
            next_radio = now;
            match pair {
                Pairing::Cancel => {
                    capture.finish();
                    radio = None;
                }
                Pairing::Start => match radio.as_mut() {
                    Some(output) => output.show_pairing_code(),
                    None => {
                        match tls_identity(&identity).and_then(|identity| {
                            radio::pair(identity, config.ble, config.names.product)
                        }) {
                            Ok(output) => radio = Some(output),
                            Err(error) => {
                                log::warn!("{} pairing: {error}", config.names.product);
                                pairing_failed = true;
                            }
                        }
                    }
                },
            }
        }
        let reporting = radio.as_ref().is_some_and(radio::Output::live);
        let fault = capture.take_sensor_failed();
        let sample = (reporting && (fault || now >= next_report)).then(|| {
            next_report = now + interval;
            snapshot(now)
        });
        tiles.select(tile, now);
        let mut phone_tile = None;
        let view = match radio.as_mut() {
            Some(output) => {
                let samples_ready = capture.take_ready();
                if output.take_ready() || samples_ready || now >= next_radio {
                    phone_tile = output.service(capture, now, tiles.phone_request());
                    next_radio = output.next_service(now, now + ACTIVE_POLL);
                }
                if let Some(snapshot) = &sample {
                    output.offer(snapshot, now, tiles.phone_request());
                }
                if output.live() {
                    View {
                        link: LinkStatus::Connected,
                        key: None,
                    }
                } else if output.paired() {
                    View {
                        link: LinkStatus::Paired,
                        key: None,
                    }
                } else {
                    View {
                        link: LinkStatus::Pairing,
                        key: Some(*output.key()),
                    }
                }
            }
            None if pairing_failed => unavailable(),
            None => View::default(),
        };
        let previous = std::mem::replace(&mut shared.lock().unwrap().view, view);
        if view != previous {
            notify();
        }
        if let Some((collector, modem, reports)) = &mut cellular {
            let (generation, socket) = modem.receive();
            if generation != link_generation {
                link_generation = generation;
                upload = None;
                reports.reset_stream();
                retry = uplink::Retry::new();
                retry_pending = false;
            }
            let progress = if let Some(socket) = socket {
                tls_identity(&identity)
                    .and_then(|identity| {
                        uplink::Upload::open(identity, socket, *collector, config.names.product)
                    })
                    .map(|connection| {
                        upload = Some(connection);
                        reports.reset_stream();
                        false
                    })
            } else {
                upload.as_mut().map_or(Ok(false), |connection| {
                    connection.service(reports, &mut snapshot)
                })
            };
            match progress {
                Ok(true) => retry = uplink::Retry::new(),
                Ok(false) => {}
                Err(error) => {
                    log::warn!("{} collector retry: {error}", config.names.product);
                    upload = None;
                    reports.reset_stream();
                    retry.later();
                    retry_pending = true;
                }
            }
            if retry_pending && now >= retry.next {
                modem.request_socket(generation);
                retry_pending = false;
            }
        }
        if let Some(Response::Tile(tile)) = tiles.service(
            || tls_identity(&identity),
            view.link == LinkStatus::Connected,
            phone_tile,
            now,
        ) {
            let mut inbox = shared.lock().unwrap();
            if inbox.tile_request == Some(tile.key) {
                inbox.tile = Some(tile);
                drop(inbox);
                notify();
            }
        }
        let mut wake = reporting.then_some(next_report);
        let active_radio = radio.as_ref().is_some_and(radio::Output::active);
        if radio.is_some() {
            wake = Some(wake.map_or(next_radio, |deadline| deadline.min(next_radio)));
        }
        if active_radio || upload.is_some() || tiles.busy() || capture.retirement_pending() {
            let poll = Instant::now() + ACTIVE_POLL;
            wake = Some(wake.map_or(poll, |deadline| deadline.min(poll)));
        }
        if retry_pending {
            wake = Some(wake.map_or(retry.next, |deadline| deadline.min(retry.next)));
        }
        // Clear/recheck the retained publication hint before sleeping. A
        // following publication leaves an unpark token; skipped wake-adapter
        // acquisition is bounded by active/pending work's polling deadline.
        if capture.take_ready() {
            next_radio = Instant::now();
            continue;
        }
        match wake {
            Some(deadline) => {
                thread::park_timeout(deadline.saturating_duration_since(Instant::now()))
            }
            None => thread::park(),
        }
    }
    capture.finish();
}

#[cfg(test)]
mod tests {
    use super::*;
    use navigation::observations::{
        publication, Observation, ObservationPublishers, ObservationReader, Reading,
    };
    use std::sync::mpsc;

    fn telemetry() -> (
        ObservationPublishers<2, 2>,
        ObservationReader<2>,
        Option<Telemetry>,
        [CapturePublisher; 3],
    ) {
        let (publishers, [display, readings]) =
            publication::<2, 2>(Duration::from_secs(1), Some(Duration::from_secs(3)));
        let (capture, capture_publishers) = export::tests::fixture([None; 2]);
        (
            publishers,
            display,
            Some(Telemetry {
                readings,
                capture,
                interval: Duration::from_millis(500),
            }),
            capture_publishers,
        )
    }

    fn test_config() -> Config {
        let service = ble::peripheral::Service {
            uuid: ble::Uuid([0; 16]),
            ingress: ble::Uuid([1; 16]),
            egress: ble::Uuid([2; 16]),
        };
        Config {
            ble: BleServices {
                pairing: service,
                telemetry: service,
            },
            pair_on_start: false,
            cellular: None,
            tile_relay: None,
            names: Names::default(),
        }
    }

    fn sample(sequence: u64) -> imu::ImuSample {
        imu::ImuSample {
            sequence,
            timestamp_us: sequence * 10_000,
            is_capture_time: false,
            sample_interval_us: 10_000,
            accel_mps2: [0.0, 0.0, 9.8],
            gyro_rps: [0.0; 3],
        }
    }

    #[test]
    fn failed_spawn_preserves_the_reader_for_late_start_and_joined_reuse() {
        let (publishers, mut display, mut pending, _capture_publishers) = telemetry();
        let [mut publisher, _] = publishers.imu;
        let now = Instant::now();
        publisher
            .record(&Observation::imu(0, imu::Event::Sample(sample(1)), now))
            .unwrap();
        let error = Communications::start_owner(&mut pending, |_| {
            Err(io::Error::other("thread creation failed"))
        })
        .err()
        .unwrap();
        assert_eq!(error.to_string(), "thread creation failed");
        assert_eq!(
            pending.as_mut().unwrap().snapshot(now).imu[0],
            Reading::Live(sample(1))
        );
        assert_eq!(display.snapshot(now).imu[0], Reading::Live(sample(1)));

        for sequence in 2..=3 {
            publisher
                .record(&Observation::imu(
                    0,
                    imu::Event::Sample(sample(sequence)),
                    now,
                ))
                .unwrap();
            // Run the real owner on the normal host test stack. The product's
            // fixed stack requires optimized target qualification separately.
            let mut links = Communications::start_owner(&mut pending, |incoming| {
                thread::Builder::new().spawn(move || {
                    let mut owner = TelemetryOwner::take(&incoming);
                    run(
                        &incoming,
                        test_config(),
                        owner.telemetry.as_mut().unwrap(),
                        &mut || {},
                    )
                })
            })
            .unwrap();
            assert!(pending.is_none());
            assert!(links.take_telemetry().is_none());
            links.stop().unwrap();
            links.stop().unwrap();
            pending = links.take_telemetry();
            assert!(links.take_telemetry().is_none());
            assert_eq!(
                pending.as_mut().unwrap().snapshot(now).imu[0],
                Reading::Live(sample(sequence))
            );
        }
    }

    #[test]
    fn paused_capture_survives_failed_spawn_stop_and_joined_owner_reuse() {
        use super::export::tests::{decode, imu, pause_record};
        let (_, _, mut pending, [primary, mut secondary, _gnss]) = telemetry();
        pending.as_mut().unwrap().capture.enable().unwrap();
        let paused = pause_record(primary, imu(0, 10, 100));
        pending.as_mut().unwrap().capture.finish();
        assert!(pending.as_ref().unwrap().capture.retirement_pending());
        assert!(Communications::start_owner(&mut pending, |_| {
            Err(io::Error::other("thread creation failed"))
        })
        .is_err());
        assert!(!pending.as_mut().unwrap().capture.enable().unwrap());

        // Stop/join must return the owner without waiting for that acquisition
        // task. The actual worker still registers its destination and runs serve.
        let (started, attached) = mpsc::channel();
        let mut links = Communications::start_owner(&mut pending, move |incoming| {
            thread::Builder::new().spawn(move || {
                let mut owner = TelemetryOwner::take(&incoming);
                started.send(()).unwrap();
                run(
                    &incoming,
                    test_config(),
                    owner.telemetry.as_mut().unwrap(),
                    &mut || {},
                )
            })
        })
        .unwrap();
        attached.recv_timeout(Duration::from_secs(2)).unwrap();
        links.stop().unwrap();
        pending = links.take_telemetry();
        assert!(pending.as_ref().unwrap().capture.retirement_pending());
        assert!(!pending.as_mut().unwrap().capture.enable().unwrap());

        // A later real owner services pending cleanup even without a radio.
        // A deliberate initial view change lets the test observe its first turn.
        let (serviced, turns) = mpsc::channel();
        let mut links = Communications::start_owner(&mut pending, move |incoming| {
            incoming.lock().unwrap().view.link = LinkStatus::Connected;
            thread::Builder::new().spawn(move || {
                let mut owner = TelemetryOwner::take(&incoming);
                run(
                    &incoming,
                    test_config(),
                    owner.telemetry.as_mut().unwrap(),
                    &mut || {
                        let _ = serviced.send(());
                    },
                )
            })
        })
        .unwrap();
        turns.recv_timeout(Duration::from_secs(2)).unwrap();
        links.shared.lock().unwrap().view.link = LinkStatus::Connected;
        let _primary = paused.release();
        turns.recv_timeout(Duration::from_secs(2)).unwrap();
        links.stop().unwrap();
        let mut returned = links.take_telemetry().unwrap();
        assert!(!returned.capture.retirement_pending());
        assert!(returned.capture.enable().unwrap());
        secondary.record(imu(1, 20, 200));
        let mut bytes = [0; export::MAX_BYTES];
        let batch = returned
            .capture
            .take(&mut bytes, true, export::Version::Timed)
            .unwrap()
            .unwrap();
        let records = decode(&bytes[..batch.len], [None; 2]);
        assert_eq!(records.len(), 1);
        assert_eq!((records[0].source, records[0].sequence), (1, 20));
    }

    #[test]
    fn the_running_reader_does_not_need_the_communications_control_mutex() {
        let (publishers, _, mut pending, _capture_publishers) = telemetry();
        let [mut publisher, _] = publishers.imu;
        let now = Instant::now();
        let (started, attached) = mpsc::channel();
        let (go, read) = mpsc::channel();
        let (sampled, observation) = mpsc::channel();
        let mut links = Communications::start_owner(&mut pending, move |incoming| {
            thread::Builder::new().spawn(move || {
                let mut owner = TelemetryOwner::take(&incoming);
                started.send(()).unwrap();
                read.recv().unwrap();
                sampled
                    .send(owner.telemetry.as_mut().unwrap().snapshot(now).imu[0])
                    .unwrap();
                Ok(())
            })
        })
        .unwrap();
        attached.recv_timeout(Duration::from_secs(2)).unwrap();
        let control = links.shared.lock().unwrap();
        publisher
            .record(&Observation::imu(0, imu::Event::Sample(sample(9)), now))
            .unwrap();
        go.send(()).unwrap();
        assert_eq!(
            observation.recv_timeout(Duration::from_secs(2)).unwrap(),
            Reading::Live(sample(9))
        );
        drop(control);
        links.stop().unwrap();
        assert!(links.take_telemetry().is_some());
    }

    #[test]
    fn error_and_unwinding_workers_return_the_reader_after_join() {
        for panic in [false, true] {
            let (_, _, mut pending, _capture_publishers) = telemetry();
            let mut links = Communications::start_owner(&mut pending, move |incoming| {
                thread::Builder::new().spawn(move || {
                    let _owner = TelemetryOwner::take(&incoming);
                    if panic {
                        let _control = incoming.lock().unwrap();
                        panic!("fixture worker failure");
                    }
                    Err(io::Error::other("fixture initialization failure"))
                })
            })
            .unwrap();
            assert!(links.stop().is_err());
            let mut returned = links.take_telemetry().unwrap();
            assert_eq!(returned.snapshot(Instant::now()).imu, [Reading::Waiting; 2]);
            assert!(links.take_telemetry().is_none());
        }
    }

    #[test]
    fn joined_owner_controls_are_quiescent_and_state_is_unavailable() {
        let (_, _, mut pending, _publishers) = telemetry();
        let mut links = Communications::start_owner(&mut pending, |incoming| {
            thread::Builder::new().spawn(move || {
                let _owner = TelemetryOwner::take(&incoming);
                Ok(())
            })
        })
        .unwrap();
        links.stop().unwrap();
        links.pair(Pairing::Start);
        links.pair(Pairing::Cancel);
        assert_eq!(links.view(View::default()).link, LinkStatus::Unavailable);
        assert!(links
            .map(Some(Key {
                zoom: maps::Zoom::default(),
                x: 1,
                y: 2,
            }))
            .is_none());
        assert!(links.shared.lock().unwrap().pairing.is_none());
        assert!(links.shared.lock().unwrap().tile_request.is_none());
        assert!(links.take_telemetry().is_some());
    }
}
