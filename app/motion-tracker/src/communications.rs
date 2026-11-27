//! Motion Tracker's communications workflow and endpoint ownership.
//! The product coordinates BLE, cellular summaries and map routing on one worker.
#[cfg(test)]
mod tests;
mod tiles;
mod uplink;

use ble::{peripheral::Service, Uuid};
use maps::{Key, Tile};
use navigation::observations::ObservationReader;
use std::cell::OnceCell;
use std::io;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use telemetry::{
    collector, radio, BleServices, BleStatus, BleView, Capture, CaptureBudget, CaptureConfig,
    CaptureView, Endpoint, Snapshot,
};
use tiles::Response;
pub use uplink::{Collector, Config as CellularConfig};

const ACTIVE_POLL: Duration = Duration::from_millis(15);
const MAX_REPORT_INTERVAL: Duration = Duration::from_secs(86_400);

pub fn config(
    pair_on_start: bool,
    cellular: Option<CellularConfig>,
    tile_relay: Option<Endpoint>,
) -> Config {
    let service = |base| Service {
        uuid: uuid(base),
        ingress: uuid(base + 1),
        egress: uuid(base + 2),
    };
    Config {
        ble: BleServices {
            pairing: service(0),
            telemetry: service(0x20),
        },
        pair_on_start,
        cellular,
        tile_relay,
        names: Names {
            product: "motion-tracker",
            worker: "tracker-links",
            collector_worker: "tracker-collector",
            tile_worker: "tracker-tile-dial",
        },
    }
}

fn uuid(last: u8) -> Uuid {
    Uuid([
        0x3c, 0xb1, 0xde, 0x6e, 0xa9, 0x55, 0x49, 0xb7, 0xbe, 0x4f, 0xc4, 0x57, 0x2d, 0x3f, 0x34,
        last,
    ])
}

pub fn capture(
    scales: [Option<[u32; 2]>; 2],
    imu_rates_millihz: [Option<u32>; 2],
    reserve_export: bool,
) -> CaptureConfig {
    CaptureConfig {
        scales,
        imu_rates_millihz,
        // The selected product profiles configure a 500 ms navigation period.
        // This is a product assumption, not a rate discovered by the GNSS HAL.
        gnss_rate_millihz: 2_000,
        reserve_export,
        budget: CaptureBudget {
            max_requested_bytes: 22 * 1024,
            stall_ms: 2_000,
            burst_records: 64,
        },
    }
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

pub enum Pairing {
    Start,
    Cancel,
}

#[derive(Default)]
struct Shared {
    stopping: bool,
    pairing: Option<Pairing>,
    view: BleView,
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
        capture: &CaptureView,
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

    pub fn view(&self, previous: BleView) -> BleView {
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

fn unavailable() -> BleView {
    BleView {
        link: BleStatus::Unavailable,
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
    modem: Option<(&uplink::Modem, Option<Collector>)>,
    telemetry: &mut Telemetry,
    notify: &mut impl FnMut(),
) {
    let identity = OnceCell::new();
    let capture_view = telemetry.capture.view();
    let capture = &mut telemetry.capture;
    let mut summaries = Summaries::new(telemetry.interval, Instant::now());
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
    let mut cellular = modem.and_then(|(modem, collector)| {
        collector.map(|collector| {
            (
                collector.endpoint,
                modem,
                collector::Reports::new(collector.interval, Instant::now())
                    .expect("report interval validated before spawn"),
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
        if capture.take_sensor_failed() {
            // Retain the fault's report until the one-second allowance opens.
            // The observation reader owns the corresponding sticky state.
            summaries.sensor_failed();
        }
        tiles.select(tile, now);
        let mut phone_tile = None;
        let view = match radio.as_mut() {
            Some(output) => {
                let samples_ready = capture.take_ready();
                if output.take_ready() || samples_ready || now >= next_radio {
                    phone_tile = output.service(capture, now, tiles.phone_request());
                    next_radio = output.next_service(now, now + ACTIVE_POLL);
                }
                if output.live()
                    && summaries.due(now, output.needs_tile_advertisement(tiles.phone_request()))
                    && output.offer(|| snapshot(now))
                {
                    summaries.accepted(now);
                }
                output.view()
            }
            None if pairing_failed => unavailable(),
            None => BleView::default(),
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
                        collector::Upload::open(
                            identity,
                            socket,
                            collector.pin,
                            config.names.product,
                        )
                    })
                    .map(|connection| {
                        upload = Some(connection);
                        reports.reset_stream();
                        false
                    })
            } else {
                upload.as_mut().map_or(Ok(false), |connection| {
                    connection.service(reports, now, || snapshot(now))
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
            view.link == BleStatus::Connected,
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
        let reporting = radio.as_ref().is_some_and(radio::Output::live);
        // Backpressure must not turn an overdue summary into a busy wait.
        // Active transport work retains the bounded poll below.
        let mut wake = reporting
            .then_some(summaries.next)
            .filter(|deadline| *deadline > now);
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

/// Tracker sends at most one ordinary BLE summary per second. A changed map
/// request may advertise immediately; an accepted advertisement also spends the
/// next summary interval. Faults may report sooner than a slow configured
/// interval, while respecting the one-second floor. Backpressure never spends
/// the allowance.
struct Summaries {
    interval: Duration,
    next: Instant,
    earliest: Instant,
}

impl Summaries {
    fn new(interval: Duration, now: Instant) -> Self {
        Self {
            interval: interval.max(Duration::from_secs(1)),
            next: now,
            earliest: now,
        }
    }

    fn due(&self, now: Instant, advertise_tile: bool) -> bool {
        now >= self.next || advertise_tile
    }

    fn accepted(&mut self, now: Instant) {
        self.next = now + self.interval;
        self.earliest = now + Duration::from_secs(1);
    }

    fn sensor_failed(&mut self) {
        self.next = self.next.min(self.earliest);
    }
}
