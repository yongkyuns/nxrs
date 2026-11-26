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
}

pub(super) struct Telemetry {
    readings: FixedReader<2>,
    capture: Arc<Capture>,
    interval: Duration,
}

impl Telemetry {
    pub fn new(readings: FixedReader<2>, capture: Arc<Capture>, interval: Duration) -> Self {
        Self {
            readings,
            capture,
            interval,
        }
    }
}

fn snapshot(readings: &mut FixedReader<2>, capture: &Capture, now: Instant) -> Snapshot {
    let mut snapshot = Snapshot::from(readings.snapshot(now));
    snapshot.clock = capture.clock();
    snapshot
}

pub struct Communications {
    shared: Arc<Mutex<Shared>>,
    task: Option<JoinHandle<io::Result<()>>>,
}

impl Communications {
    pub fn start(
        pair: bool,
        map: bool,
        cellular: Option<CellularConfig>,
        tile_relay: Option<Endpoint>,
        telemetry: Telemetry,
        mut notify: impl FnMut() + Send + 'static,
    ) -> io::Result<Self> {
        if ble::peripheral::AVAILABLE && (pair || map) {
            telemetry.capture.prepare()?;
        }
        let shared = Arc::new(Mutex::new(Shared::default()));
        let incoming = shared.clone();
        let task = thread::Builder::new()
            .name("tracker-links".into())
            .stack_size(23 * 1024)
            .spawn(move || {
                let result = run(
                    &incoming,
                    pair,
                    map,
                    cellular,
                    tile_relay,
                    telemetry,
                    &mut notify,
                );
                if let Err(error) = &result {
                    incoming.lock().unwrap().view = unavailable();
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
            self.shared.lock().unwrap().stopping = true;
            task.thread().unpark();
            return task
                .join()
                .map_err(|_| io::Error::other("tracker links task panicked"))?;
        }
        Ok(())
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

fn tls_identity(identity: Option<&tls::Identity>) -> io::Result<&tls::Identity> {
    identity.ok_or_else(|| io::ErrorKind::Unsupported.into())
}

fn run(
    shared: &Mutex<Shared>,
    pair: bool,
    map: bool,
    cellular: Option<CellularConfig>,
    tile_relay: Option<Endpoint>,
    mut telemetry: Telemetry,
    notify: &mut impl FnMut(),
) -> io::Result<()> {
    // Use the profile's normal worker priority for link progress.
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
    let radio = ble::peripheral::AVAILABLE && (pair || map);
    if radio {
        radio::prepare().map_err(|error| io::Error::other(format!("BLE setup: {error:?}")))?;
    }
    let mut identity = None;
    let slots = usize::from(radio) + usize::from(collector.is_some() || tile_relay.is_some());
    if slots != 0 {
        let mut tls =
            tls::open().map_err(|error| io::Error::other(format!("TLS setup: {error:?}")))?;
        tls.reserve_reconnects(slots)
            .map_err(|error| io::Error::other(format!("TLS reconnect storage: {error:?}")))?;
        identity = Some(tls);
    }
    let mut modem = cellular.map(uplink::Modem::start).transpose()?;
    let map = map
        .then(tiles::Pool::new)
        .transpose()
        .unwrap_or_else(|error| {
            log::warn!("motion-tracker map storage unavailable: {error}");
            None
        });
    serve(
        shared,
        pair,
        map,
        modem.as_ref().map(|modem| (modem, collector)),
        tile_relay,
        identity.as_ref(),
        &mut telemetry,
        notify,
    );
    // Both transports and their shared TLS identity have left scope.
    modem.as_mut().map_or(Ok(()), uplink::Modem::stop)
}

fn serve(
    shared: &Mutex<Shared>,
    pair: bool,
    map: Option<tiles::Pool>,
    modem: Option<(&uplink::Modem, Option<uplink::Collector>)>,
    tile_relay: Option<Endpoint>,
    identity: Option<&tls::Identity>,
    telemetry: &mut Telemetry,
    notify: &mut impl FnMut(),
) {
    let Telemetry {
        readings,
        capture,
        interval,
    } = telemetry;
    let mut radio: Option<radio::Output<'_, _>> = None;
    let mut tiles = tiles::Tiles::new(
        tile_relay.filter(|_| map.is_some()),
        map.clone(),
        Instant::now(),
    );
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
            let tile = inbox.tile_request.filter(|key| {
                map.is_some() && inbox.tile.as_ref().is_none_or(|tile| tile.key != *key)
            });
            (inbox.pairing.take().or(command.take()), tile)
        };
        let now = Instant::now();
        if let Some((modem, _)) = modem {
            let status = modem.status();
            if cellular_status != Some(status) {
                log::info!("motion-tracker cellular {status:?}");
                cellular_status = Some(status);
            }
        }
        if radio.as_ref().is_some_and(|output| output.expired(now)) {
            radio = None;
        }
        if let Some(pair) = pair {
            pairing_failed = false;
            next_radio = now;
            match pair {
                Pairing::Cancel => radio = None,
                Pairing::Start => match radio.as_mut() {
                    Some(output) => output.show_pairing_code(),
                    None => match tls_identity(identity)
                        .and_then(|identity| radio::pair(identity, capture, map.clone()))
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
        let fault = capture.take_sensor_failed();
        let sample = (reporting && (fault || now >= next_report)).then(|| {
            next_report = now + *interval;
            snapshot(readings, capture, now)
        });
        tiles.select(tile, now);
        let mut phone_tile = None;
        let view = match radio.as_mut() {
            Some(output) => {
                let samples_ready = capture.take_ready();
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
                tls_identity(identity)
                    .and_then(|identity| uplink::Upload::open(identity, socket, *collector))
                    .map(|connection| {
                        upload = Some(connection);
                        reports.reset_stream();
                        false
                    })
            } else {
                upload.as_mut().map_or(Ok(false), |connection| {
                    connection.service(reports, |now| snapshot(readings, capture, now))
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
            || tls_identity(identity),
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
        if radio.is_some() && !active_radio {
            wake = Some(wake.map_or(next_radio, |deadline| deadline.min(next_radio)));
        }
        if active_radio || upload.is_some() || tiles.busy() {
            let poll = Instant::now() + ACTIVE_POLL;
            wake = Some(wake.map_or(poll, |deadline| deadline.min(poll)));
        }
        if retry_pending {
            wake = Some(wake.map_or(retry.next, |deadline| deadline.min(retry.next)));
        }
        match wake {
            Some(deadline) => {
                thread::park_timeout(deadline.saturating_duration_since(Instant::now()))
            }
            None => thread::park(),
        }
    }
}
