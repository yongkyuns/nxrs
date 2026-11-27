//! One task steps both TLS sessions under the SDK's single identity lease.
mod export;
mod publication;
mod radio;
mod tiles;
mod uplink;
mod wire;

pub(super) use export::Capture;
pub(super) use tiles::{Key, Tile, Zoom, DIRECT_RELAY_AVAILABLE, SIDE};
pub(super) use uplink::{Collector, Config as CellularConfig, Endpoint};
pub(super) use wire::write_usb;

use crate::tracker::Snapshot;
use navigation::observations::FixedReader;
use std::cell::{Cell, OnceCell};
use std::io;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tiles::Response;

const ACTIVE_POLL: Duration = Duration::from_millis(15);

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
    pub cellular: Option<cellular::Status>,
}

impl Default for View {
    fn default() -> Self {
        Self {
            link: LinkStatus::Idle,
            key: None,
            cellular: None,
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
}

#[derive(Clone)]
pub(super) struct Telemetry {
    // Keep the reader available if spawning the communications worker fails.
    readings: Arc<Mutex<FixedReader<2>>>,
    capture: Arc<Capture>,
    interval: Duration,
    status: Arc<crate::health::Status>,
}

impl Telemetry {
    pub fn new(
        readings: FixedReader<2>,
        capture: Arc<Capture>,
        interval: Duration,
        status: Arc<crate::health::Status>,
    ) -> Self {
        Self {
            readings: Arc::new(Mutex::new(readings)),
            capture,
            interval,
            status,
        }
    }

    fn snapshot(&self, readings: &mut FixedReader<2>, now: Instant) -> Snapshot {
        let mut snapshot = Snapshot::from(readings.snapshot(now));
        snapshot.clock = self.capture.clock();
        snapshot.output_faults = self.status.snapshot();
        snapshot
    }
}

pub struct Communications {
    shared: Arc<Mutex<Shared>>,
    task: Option<JoinHandle<io::Result<()>>>,
}

impl Communications {
    pub fn start(
        pair: bool,
        cellular: Option<CellularConfig>,
        tile_relay: Option<Endpoint>,
        telemetry: Telemetry,
        mut notify: impl FnMut() + Send + 'static,
    ) -> io::Result<Self> {
        let shared = Arc::new(Mutex::new(Shared::default()));
        let incoming = shared.clone();
        let task = thread::Builder::new()
            .name("tracker-links".into())
            .stack_size(23 * 1024)
            .spawn(move || {
                let result = run(
                    &incoming,
                    pair,
                    cellular,
                    tile_relay,
                    telemetry,
                    &mut notify,
                );
                if let Err(error) = &result {
                    let mut shared = incoming.lock().unwrap();
                    shared.view = unavailable(shared.view.cellular);
                    drop(shared);
                    notify();
                    log::error!("motion-tracker communications stopped: {error}");
                }
                result
            })?;
        Ok(Self {
            shared,
            task: Some(task),
        })
    }

    pub fn pair(&self, command: Pairing) {
        self.shared.lock().unwrap().pairing = Some(command);
        if let Some(task) = &self.task {
            task.thread().unpark();
        }
    }

    pub fn view(&self, previous: View) -> View {
        let view = self
            .shared
            .try_lock()
            .map_or(previous, |shared| shared.view);
        if self.task.as_ref().is_some_and(JoinHandle::is_finished) {
            return unavailable(view.cellular);
        }
        view
    }

    pub fn map(&self, wanted: Option<Key>) -> Option<Tile> {
        let Ok(mut shared) = self.shared.try_lock() else {
            return None;
        };
        let tile = shared.tile.take();
        let wanted = wanted.filter(|key| tile.as_ref().is_none_or(|tile| tile.key != *key));
        if shared.tile_request != wanted {
            shared.tile_request = wanted;
            if let Some(task) = &self.task {
                task.thread().unpark();
            }
        }
        tile
    }

    pub fn request_stop(&mut self) {
        self.shared.lock().unwrap().stopping = true;
        if let Some(task) = &self.task {
            task.thread().unpark();
        }
    }

    pub fn is_finished(&self) -> bool {
        self.task.as_ref().is_none_or(JoinHandle::is_finished)
    }

    /// True only after the worker was joined, including a reported failure.
    pub fn is_quiescent(&self) -> bool {
        self.task.is_none()
    }

    pub fn join(&mut self) -> io::Result<()> {
        if let Some(task) = self.task.take() {
            return task
                .join()
                .map_err(|_| io::Error::other("tracker links task panicked"))?;
        }
        Ok(())
    }
}

fn unavailable(cellular: Option<cellular::Status>) -> View {
    View {
        link: LinkStatus::Unavailable,
        key: None,
        cellular,
    }
}

impl Drop for Communications {
    fn drop(&mut self) {
        // The worker owns every transport and retains their state. A
        // supervisor that reaches its shutdown deadline must not enter a
        // hidden blocking join here; only join() establishes quiescence.
        self.request_stop();
    }
}

#[derive(Default)]
struct TlsIdentity {
    identity: OnceCell<tls::Identity>,
    admission_error: Cell<Option<tls::Error>>,
}

impl TlsIdentity {
    fn get(&self) -> io::Result<&tls::Identity> {
        if self.identity.get().is_none() && self.admission_error.get().is_none() {
            match tls::open() {
                Ok(identity) => {
                    let _ = self.identity.set(identity);
                }
                Err(error) => self.admission_error.set(Some(error)),
            }
        }
        match self.identity.get() {
            Some(identity) => Ok(identity),
            None => Err(io::Error::other(format!(
                "TLS: {:?}",
                self.admission_error.get().unwrap()
            ))),
        }
    }

    fn needs_replacement(&self) -> bool {
        self.identity.get().is_some_and(tls::Identity::is_poisoned)
            || matches!(
                self.admission_error.get(),
                Some(
                    tls::Error::Busy
                        | tls::Error::Entropy
                        | tls::Error::Poisoned
                        | tls::Error::Capacity
                        | tls::Error::Crypto
                        | tls::Error::Io
                )
            )
    }
}

fn run(
    shared: &Mutex<Shared>,
    mut pair: bool,
    cellular: Option<CellularConfig>,
    tile_relay: Option<Endpoint>,
    telemetry: Telemetry,
    notify: &mut impl FnMut(),
) -> io::Result<()> {
    // Pairing from the display can spawn this worker after the display has
    // taken its fixed background priority.
    match execution::configure_current(execution::Priority::Normal) {
        Ok(()) | Err(execution::Error::Unsupported) => {}
        Err(error) => {
            return Err(io::Error::other(format!(
                "tracker links priority: {error:?}"
            )))
        }
    }
    telemetry.capture.register_owner();
    let collector = cellular.as_ref().and_then(|config| config.collector);
    let mut cellular = uplink::Cellular::new(cellular);
    let mut reports =
        collector.map(|collector| uplink::Reports::new(collector.interval, Instant::now()));
    let mut readings = telemetry.readings.lock().unwrap();
    let mut retry_identity = uplink::Retry::new();
    while serve(
        shared,
        &mut pair,
        &mut cellular,
        collector,
        reports.as_mut(),
        tile_relay,
        &telemetry,
        &mut readings,
        notify,
    ) {
        // All BLE, upload and map TLS borrowers have retired on return. Only
        // now can a fresh identity acquire the SDK lease. Trust is unchanged;
        // a new BLE identity requires a newly displayed pairing key.
        retry_identity.later();
        if !wait_identity_retry(
            shared,
            &mut pair,
            retry_identity.next,
            &mut cellular,
            notify,
        ) {
            break;
        }
        if let Some(reports) = &mut reports {
            reports.reset_stream();
        }
        if let Some(modem) = cellular.modem() {
            let (generation, retired) = modem.receive();
            drop(retired);
            modem.request_socket(generation);
        }
    }
    // Both transports and their shared TLS identity have left scope.
    let result = cellular.stop();
    let view = unavailable(cellular.status());
    let previous = std::mem::replace(&mut shared.lock().unwrap().view, view);
    if previous != view {
        notify();
    }
    result
}

fn wait_identity_retry(
    shared: &Mutex<Shared>,
    pair: &mut bool,
    deadline: Instant,
    cellular: &mut uplink::Cellular,
    notify: &mut impl FnMut(),
) -> bool {
    loop {
        {
            let mut inbox = shared.lock().unwrap();
            if inbox.stopping {
                drop(inbox);
                cellular.request_stop();
                return false;
            }
            if let Some(command) = inbox.pairing.take() {
                *pair = matches!(command, Pairing::Start);
            }
        }
        let now = Instant::now();
        cellular.admit(now);
        let status = cellular.status();
        let changed = {
            let mut inbox = shared.lock().unwrap();
            std::mem::replace(&mut inbox.view.cellular, status) != status
        };
        if changed {
            notify();
        }
        let remaining = deadline.saturating_duration_since(now);
        if remaining.is_zero() {
            return true;
        }
        let next = cellular
            .next_admission()
            .map_or(deadline, |next| deadline.min(next));
        thread::park_timeout(next.saturating_duration_since(Instant::now()));
    }
}

fn serve(
    shared: &Mutex<Shared>,
    pair_requested: &mut bool,
    cellular: &mut uplink::Cellular,
    collector: Option<uplink::Collector>,
    mut reports: Option<&mut uplink::Reports>,
    tile_relay: Option<Endpoint>,
    telemetry: &Telemetry,
    readings: &mut FixedReader<2>,
    notify: &mut impl FnMut(),
) -> bool {
    let identity = TlsIdentity::default();
    let mut radio: Option<radio::Output<'_, _>> = None;
    let mut tiles = tiles::Tiles::new(tile_relay, Instant::now());
    let mut next_radio = Instant::now();
    let mut upload = None;
    let mut retry = uplink::Retry::new();
    let mut retry_pending = false;
    let mut link_generation = 0;
    let mut pairing_failed = false;
    let mut command = pair_requested.then_some(Pairing::Start);
    let mut next_report = Instant::now();
    let mut previous_cellular_status = None;

    loop {
        let (pair, tile) = {
            let mut inbox = shared.lock().unwrap();
            if inbox.stopping {
                drop(inbox);
                cellular.request_stop();
                return false;
            }
            let tile = inbox
                .tile_request
                .filter(|key| inbox.tile.as_ref().is_none_or(|tile| tile.key != *key));
            (inbox.pairing.take().or(command.take()), tile)
        };
        let now = Instant::now();
        cellular.admit(now);
        let cellular_status = cellular.status();
        if let Some(status) = cellular_status {
            if previous_cellular_status != cellular_status {
                log::info!("motion-tracker cellular {status:?}");
                previous_cellular_status = cellular_status;
            }
        }
        if radio.as_ref().is_some_and(|output| output.expired(now)) {
            radio = None;
            *pair_requested = false;
        }
        if let Some(pair) = pair {
            *pair_requested = matches!(pair, Pairing::Start);
            pairing_failed = false;
            next_radio = now;
            match pair {
                Pairing::Cancel => radio = None,
                Pairing::Start => match radio.as_mut() {
                    Some(output) => output.show_pairing_code(),
                    None => match identity
                        .get()
                        .and_then(|identity| radio::pair(identity, &telemetry.capture))
                    {
                        Ok(output) => radio = Some(output),
                        Err(error) => {
                            log::warn!("motion-tracker pairing: {error}");
                            pairing_failed = true;
                        }
                    },
                },
            }
        }
        let reporting = radio.as_ref().is_some_and(radio::Output::live);
        let fault = telemetry.capture.take_sensor_failed();
        let sample = (reporting && (fault || now >= next_report)).then(|| {
            next_report = now + telemetry.interval;
            telemetry.snapshot(readings, now)
        });
        tiles.select(tile, now);
        let mut phone_tile = None;
        let view = match radio.as_mut() {
            Some(output) => {
                let samples_ready = telemetry.capture.take_ready();
                if output.take_ready() || samples_ready || now >= next_radio {
                    phone_tile = output.service(now, tiles.phone_request());
                    next_radio = output.next_service(now + ACTIVE_POLL);
                }
                if let Some(snapshot) = &sample {
                    output.offer(snapshot, now, tiles.phone_request());
                }
                if output.live() {
                    View {
                        link: LinkStatus::Connected,
                        key: None,
                        cellular: cellular_status,
                    }
                } else if output.paired() {
                    View {
                        link: LinkStatus::Paired,
                        key: None,
                        cellular: cellular_status,
                    }
                } else {
                    View {
                        link: LinkStatus::Pairing,
                        key: Some(*output.key()),
                        cellular: cellular_status,
                    }
                }
            }
            None if pairing_failed => unavailable(cellular_status),
            None => View {
                cellular: cellular_status,
                ..View::default()
            },
        };
        let previous = std::mem::replace(&mut shared.lock().unwrap().view, view);
        if view != previous {
            notify();
        }
        if let (Some(collector), Some(modem), Some(reports)) =
            (collector, cellular.modem(), reports.as_deref_mut())
        {
            let (generation, socket) = modem.receive();
            if generation != link_generation {
                link_generation = generation;
                upload = None;
                reports.reset_stream();
                retry = uplink::Retry::new();
                retry_pending = false;
            }
            let progress = if let Some(socket) = socket {
                identity
                    .get()
                    .and_then(|identity| uplink::Upload::open(identity, socket, collector.endpoint))
                    .map(|connection| {
                        upload = Some(connection);
                        reports.reset_stream();
                        false
                    })
            } else {
                upload.as_mut().map_or(Ok(false), |connection| {
                    connection.service(reports, |now| telemetry.snapshot(readings, now))
                })
            };
            match progress {
                Ok(true) => retry = uplink::Retry::new(),
                Ok(false) => {}
                Err(error) => {
                    log::warn!("motion-tracker collector retry: {error}");
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
            || identity.get(),
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
        if identity.needs_replacement() {
            log::warn!("motion-tracker TLS identity retired; retrying with fresh credentials");
            let view = unavailable(cellular_status);
            let previous = std::mem::replace(&mut shared.lock().unwrap().view, view);
            if previous != view {
                notify();
            }
            return true;
        }
        let mut wake = reporting.then_some(next_report);
        let active_radio = radio.as_ref().is_some_and(radio::Output::active);
        if radio.is_some() && !active_radio {
            wake = Some(wake.map_or(next_radio, |deadline| deadline.min(next_radio)));
        }
        if active_radio || upload.is_some() {
            let poll = Instant::now() + ACTIVE_POLL;
            wake = Some(wake.map_or(poll, |deadline| deadline.min(poll)));
        }
        if retry_pending {
            wake = Some(wake.map_or(retry.next, |deadline| deadline.min(retry.next)));
        }
        if let Some(next) = cellular.next_admission() {
            wake = Some(wake.map_or(next, |deadline| deadline.min(next)));
        }
        wait_for_work(wake, &tiles, view.link == LinkStatus::Connected);
    }
}

fn wait_for_work(wake: Option<Instant>, tiles: &tiles::Tiles<'_>, phone_live: bool) {
    let tile = tiles.next_service(Instant::now() + ACTIVE_POLL, phone_live);
    let wake = match (wake, tile) {
        (Some(left), Some(right)) => Some(left.min(right)),
        (left, right) => left.or(right),
    };
    match wake {
        Some(deadline) => thread::park_timeout(deadline.saturating_duration_since(Instant::now())),
        None => thread::park(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn dropping_an_unretired_owner_requests_stop_without_a_hidden_join() {
        let shared = Arc::new(Mutex::new(Shared::default()));
        let (release, held) = mpsc::channel();
        let (retired, retirement) = mpsc::channel();
        let worker = thread::spawn(move || {
            held.recv().unwrap();
            retired.send(()).unwrap();
            Ok(())
        });
        let owner = Communications {
            shared: shared.clone(),
            task: Some(worker),
        };
        assert!(!owner.is_quiescent());
        let (dropped, completion) = mpsc::channel();
        let supervisor = thread::spawn(move || {
            drop(owner);
            dropped.send(()).unwrap();
        });
        let prompt = completion.recv_timeout(Duration::from_secs(1));
        release.send(()).unwrap();
        supervisor.join().unwrap();
        retirement.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(
            prompt.is_ok(),
            "Drop must preserve the supervisor's outer deadline"
        );
        assert!(shared.lock().unwrap().stopping);
    }

    #[test]
    fn cellular_admission_failure_keeps_the_owner_available_for_pair_and_cancel() {
        if cellular::LINK_AVAILABLE || tls::Identity::SUPPORTS_PINNED_CLIENT {
            return; // Explicit unbound/mock capability witness; no hardware acquisition.
        }
        let (_, _, [_, reader]) =
            navigation::observations::fixed_readers(Duration::from_secs(1), Duration::from_secs(3));
        let telemetry = Telemetry::new(
            reader,
            Arc::new(Capture::default()),
            Duration::from_secs(1),
            Arc::new(crate::health::Status::default()),
        );
        let (sent, received) = mpsc::channel();
        let shared = Arc::new(Mutex::new(Shared::default()));
        let incoming = shared.clone();
        // Exercise the actual owner loop with a host debug-frame budget. This
        // fixture does not qualify the product's 23 KiB optimized task stack.
        let task = thread::Builder::new()
            .name("tracker-links-fixture".into())
            .stack_size(256 * 1024)
            .spawn(move || {
                run(
                    &incoming,
                    false,
                    Some(CellularConfig {
                        apn: "test".into(),
                        collector: None,
                    }),
                    None,
                    telemetry,
                    &mut || {
                        let _ = sent.send(());
                    },
                )
            })
            .unwrap();
        let mut owner = Communications {
            shared,
            task: Some(task),
        };
        received.recv_timeout(Duration::from_secs(1)).unwrap();
        let view = owner.view(View::default());
        assert_eq!(
            view.cellular,
            Some(cellular::Status::Failed(cellular::Error::Unsupported))
        );
        assert_eq!(view.link, LinkStatus::Idle);
        assert!(!owner.is_finished());
        owner.pair(Pairing::Start);
        received.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(owner.view(view).link, LinkStatus::Unavailable);
        assert!(!owner.is_finished());
        owner.pair(Pairing::Cancel);
        received.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(owner.view(view).link, LinkStatus::Idle);
        owner.request_stop();
        owner.join().unwrap();
        assert!(owner.is_quiescent());
    }

    #[test]
    fn stop_wakes_identity_backoff_without_waiting_for_its_deadline() {
        let shared = Arc::new(Mutex::new(Shared::default()));
        let worker_shared = shared.clone();
        let (sent, received) = mpsc::channel();
        let task = thread::spawn(move || {
            let mut pair = true;
            sent.send(()).unwrap();
            wait_identity_retry(
                &worker_shared,
                &mut pair,
                Instant::now() + Duration::from_secs(30),
                &mut uplink::Cellular::new(None),
                &mut || {},
            )
        });
        received.recv_timeout(Duration::from_secs(1)).unwrap();
        shared.lock().unwrap().stopping = true;
        let cancelled = Instant::now();
        task.thread().unpark();
        assert!(!task.join().unwrap());
        assert!(cancelled.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn cancellation_during_backoff_is_retained_without_bypassing_the_budget() {
        let shared = Mutex::new(Shared {
            pairing: Some(Pairing::Cancel),
            ..Shared::default()
        });
        let mut pair = true;
        let deadline = Instant::now() + Duration::from_millis(20);
        assert!(wait_identity_retry(
            &shared,
            &mut pair,
            deadline,
            &mut uplink::Cellular::new(None),
            &mut || {}
        ));
        assert!(Instant::now() >= deadline);
        assert!(!pair);
    }

    #[test]
    fn trust_and_configuration_errors_do_not_trigger_identity_replacement() {
        let identity = TlsIdentity::default();
        for error in [
            tls::Error::Unsupported,
            tls::Error::Authentication,
            tls::Error::InvalidInput,
            tls::Error::Time,
        ] {
            identity.admission_error.set(Some(error));
            assert!(!identity.needs_replacement());
        }
        for error in [
            tls::Error::Entropy,
            tls::Error::Poisoned,
            tls::Error::Busy,
            tls::Error::Capacity,
        ] {
            identity.admission_error.set(Some(error));
            assert!(identity.needs_replacement());
        }
    }
}
