//! One task steps both TLS sessions under the SDK's single identity lease.
mod export;
mod publication;
mod radio;
mod tiles;
mod uplink;
mod wire;

pub(super) use export::{Capture, CapturePublisher};
pub(super) use tiles::{Key, Tile, Zoom, DIRECT_RELAY_AVAILABLE, SIDE};
pub(super) use uplink::{Collector, Config as CellularConfig, Endpoint};
pub(super) use wire::write_usb;

use crate::tracker::Snapshot;
use navigation::observations::ObservationReader;
use std::cell::OnceCell;
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

pub(super) struct Telemetry {
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
        pair: bool,
        cellular: Option<CellularConfig>,
        tile_relay: Option<Endpoint>,
        telemetry: &mut Option<Telemetry>,
        mut notify: impl FnMut() + Send + 'static,
    ) -> io::Result<Self> {
        Self::start_owner(telemetry, move |incoming| {
            thread::Builder::new()
                .name("tracker-links".into())
                .stack_size(23 * 1024)
                .spawn(move || {
                    let mut owner = TelemetryOwner::take(&incoming);
                    let result = run(
                        &incoming,
                        pair,
                        cellular,
                        tile_relay,
                        owner.telemetry.as_mut().unwrap(),
                        &mut notify,
                    );
                    if let Err(error) = &result {
                        incoming.lock().unwrap().view = unavailable();
                        notify();
                        log::error!("motion-tracker communications stopped: {error}");
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
        self.shared.lock().unwrap().pairing = Some(command);
        self.task.as_ref().unwrap().thread().unpark();
    }

    pub fn view(&self, previous: View) -> View {
        if self.task.as_ref().is_some_and(JoinHandle::is_finished) {
            return unavailable();
        }
        self.shared
            .try_lock()
            .map_or(previous, |shared| shared.view)
    }

    pub fn map(&self, wanted: Option<Key>) -> Option<Tile> {
        let Ok(mut shared) = self.shared.try_lock() else {
            return None;
        };
        let tile = shared.tile.take();
        let wanted = wanted.filter(|key| tile.as_ref().is_none_or(|tile| tile.key != *key));
        if shared.tile_request != wanted {
            shared.tile_request = wanted;
            self.task.as_ref().unwrap().thread().unpark();
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
                .map_err(|_| io::Error::other("tracker links task panicked"))?;
        }
        Ok(())
    }

    /// Recover the fixed reader only after joining its previous owner. A later
    /// start may reuse it without adding a subscriber or losing unread values.
    /// Capture can still be retiring a paused acquisition callback: the joined
    /// owner returns promptly and the next owner drains before opening an epoch.
    /// Full application shutdown joins acquisition before retiring this endpoint.
    pub(super) fn take_telemetry(&mut self) -> Option<Telemetry> {
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
    pair: bool,
    cellular: Option<CellularConfig>,
    tile_relay: Option<Endpoint>,
    telemetry: &mut Telemetry,
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
    let mut modem = cellular.map(uplink::Modem::start).transpose()?;
    serve(
        shared,
        pair,
        modem.as_ref().map(|modem| (modem, collector)),
        tile_relay,
        telemetry,
        notify,
    );
    // Both transports and their shared TLS identity have left scope.
    modem.as_mut().map_or(Ok(()), uplink::Modem::stop)
}

fn serve(
    shared: &Mutex<Shared>,
    pair: bool,
    modem: Option<(&uplink::Modem, Option<uplink::Collector>)>,
    tile_relay: Option<Endpoint>,
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
    let mut tiles = tiles::Tiles::new(tile_relay, Instant::now());
    let mut next_radio = Instant::now();
    let mut upload = None;
    let mut retry = uplink::Retry::new();
    let mut retry_pending = false;
    let mut link_generation = 0;
    let mut pairing_failed = false;
    let mut command = pair.then_some(Pairing::Start);
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
                log::info!("motion-tracker cellular {status:?}");
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
                        match tls_identity(&identity).and_then(|identity| radio::pair(identity)) {
                            Ok(output) => radio = Some(output),
                            Err(error) => {
                                log::warn!("motion-tracker pairing: {error}");
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
                    .and_then(|identity| uplink::Upload::open(identity, socket, *collector))
                    .map(|connection| {
                        upload = Some(connection);
                        reports.reset_stream();
                        false
                    })
            } else {
                upload.as_mut().map_or(Ok(false), |connection| {
                    connection.service(reports, |now| snapshot(now))
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
        let (capture, capture_publishers) =
            Capture::new([None; 2], [Some(100_000); 2], true).unwrap();
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
                        false,
                        None,
                        None,
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
                    false,
                    None,
                    None,
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
                    false,
                    None,
                    None,
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
}
