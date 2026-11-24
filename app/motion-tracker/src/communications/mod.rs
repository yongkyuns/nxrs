//! One task steps both TLS sessions under the SDK's single identity lease.
mod config;
#[cfg(feature = "ble-link")]
mod export;
#[cfg(feature = "ble-link")]
mod radio;
#[cfg(any(feature = "ble-link", feature = "wifi-map"))]
mod tiles;

pub(super) use config::CellularConfig;
pub(super) use connectivity::Endpoint;
#[cfg(feature = "ble-link")]
pub(super) use export::Capture;

#[cfg(any(feature = "ble-link", feature = "wifi-map"))]
use crate::map::Response;
use crate::{
    map::{Key, Tile},
    telemetry::Telemetry,
};
use std::io;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
#[cfg(any(
    feature = "ble-link",
    feature = "cellular-uplink",
    feature = "wifi-map"
))]
use std::{cell::OnceCell, time::Instant};

#[cfg(any(feature = "ble-link", feature = "wifi-map"))]
const ACTIVE_POLL: std::time::Duration = std::time::Duration::from_millis(15);
pub const BLE_AVAILABLE: bool = cfg!(feature = "ble-link");
pub const CELLULAR_AVAILABLE: bool = cfg!(feature = "cellular-uplink");
pub const DIRECT_MAP_AVAILABLE: bool = cfg!(feature = "wifi-map") && !CELLULAR_AVAILABLE;

pub fn validate(pair: bool, cellular: bool, direct_map: bool) -> io::Result<()> {
    let unsupported = |message| io::Error::new(io::ErrorKind::Unsupported, message);
    if pair && !BLE_AVAILABLE {
        return Err(unsupported("BLE is unavailable in this build"));
    }
    if cellular && !CELLULAR_AVAILABLE {
        return Err(unsupported("cellular is unavailable in this build"));
    }
    if direct_map && !DIRECT_MAP_AVAILABLE {
        return Err(unsupported(
            "direct map relay requires a Wi-Fi build without cellular",
        ));
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg_attr(not(feature = "ble-link"), allow(dead_code))]
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

#[allow(dead_code, reason = "each link build emits only its supported updates")]
pub(super) enum Update {
    Links,
    Report,
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
        notify: impl FnMut(Update) + Send + 'static,
    ) -> io::Result<Self> {
        validate(pair, cellular.is_some(), tile_relay.is_some())?;
        let shared = Arc::new(Mutex::new(Shared::default()));
        let incoming = shared.clone();
        // Keep a startup slot so a failed spawn can return acquisition to the dashboard.
        let startup = Arc::new(Mutex::new(telemetry.take()));
        let handoff = startup.clone();
        let task = thread::Builder::new()
            .name("tracker-links".into())
            .stack_size(23 * 1024)
            .spawn(move || {
                let telemetry = handoff
                    .lock()
                    .unwrap()
                    .take()
                    .expect("local telemetry owner");
                drop(handoff);
                run(&incoming, pair, cellular, tile_relay, telemetry, notify)
            })
            .map_err(|error| {
                *telemetry = startup.lock().unwrap().take();
                error
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

    pub fn is_finished(&self) -> bool {
        self.task.as_ref().is_some_and(JoinHandle::is_finished)
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

impl Drop for Communications {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

#[cfg(any(
    feature = "ble-link",
    feature = "cellular-uplink",
    feature = "wifi-map"
))]
fn tls_identity(slot: &OnceCell<tls::Identity>) -> io::Result<&tls::Identity> {
    if slot.get().is_none() {
        let identity = tls::open().map_err(|error| io::Error::other(format!("TLS: {error:?}")))?;
        let _ = slot.set(identity);
    }
    Ok(slot.get().unwrap())
}

#[cfg(not(any(
    feature = "ble-link",
    feature = "cellular-uplink",
    feature = "wifi-map"
)))]
fn run(
    _: &Mutex<Shared>,
    _: bool,
    _: Option<CellularConfig>,
    _: Option<Endpoint>,
    mut telemetry: Telemetry,
    _: impl FnMut(Update),
) -> io::Result<()> {
    telemetry.stop().and(Err(io::ErrorKind::Unsupported.into()))
}

#[cfg(any(
    feature = "ble-link",
    feature = "cellular-uplink",
    feature = "wifi-map"
))]
fn run(
    shared: &Mutex<Shared>,
    pair: bool,
    cellular: Option<CellularConfig>,
    tile_relay: Option<Endpoint>,
    mut telemetry: Telemetry,
    mut notify: impl FnMut(Update),
) -> io::Result<()> {
    #[cfg(feature = "ble-link")]
    let capture = telemetry.capture();
    #[cfg(feature = "ble-link")]
    capture.register_owner();
    #[cfg(not(feature = "ble-link"))]
    let _ = pair;
    #[cfg(not(feature = "cellular-uplink"))]
    let _ = cellular;
    #[cfg(not(any(feature = "ble-link", feature = "wifi-map")))]
    let _ = tile_relay;
    let identity = OnceCell::new();
    #[cfg(feature = "cellular-uplink")]
    let mut uplink = {
        use connectivity::cellular::{Budget, Destination, Uplink};
        cellular
            .map(|config| {
                let budget = Budget::new(config.interval, crate::wire::cellular::MAX_BYTES)?;
                let destination = config
                    .collector
                    .map(|endpoint| Destination { endpoint, budget });
                let worker = thread::Builder::new()
                    .name("tracker-modem".into())
                    .stack_size(4 * 1024);
                Uplink::start(worker, destination, move || cellular::connect(&config.apn))
            })
            .transpose()?
    };
    #[cfg(feature = "ble-link")]
    let mut radio: Option<radio::Output<'_, _>> = None;
    #[cfg(any(feature = "ble-link", feature = "wifi-map"))]
    let mut tiles = tiles::Tiles::new(tile_relay, Instant::now());
    #[cfg(feature = "ble-link")]
    let mut next_radio = Instant::now();
    #[cfg(feature = "ble-link")]
    let mut pairing_failed = false;
    #[cfg(feature = "ble-link")]
    let mut command = pair.then_some(Pairing::Start);

    loop {
        let (pair, tile) = {
            let mut inbox = shared.lock().unwrap();
            if inbox.stopping {
                break;
            }
            let tile = inbox
                .tile_request
                .filter(|key| inbox.tile.as_ref().is_none_or(|tile| tile.key != *key));
            (inbox.pairing.take(), tile)
        };
        let now = Instant::now();
        let (sample, report_ready) = telemetry.step(now);
        if report_ready {
            notify(Update::Report);
        }
        #[cfg(not(feature = "ble-link"))]
        let _ = pair;
        #[cfg(not(any(feature = "ble-link", feature = "wifi-map")))]
        let _ = tile;
        #[cfg(not(any(feature = "ble-link", feature = "cellular-uplink")))]
        let _ = sample;
        #[cfg(feature = "ble-link")]
        if radio.as_ref().is_some_and(|output| output.expired(now)) {
            radio = None;
        }
        #[cfg(feature = "ble-link")]
        if let Some(pair) = pair.or(command.take()) {
            pairing_failed = false;
            match pair {
                Pairing::Cancel => radio = None,
                Pairing::Start => match radio.as_mut() {
                    Some(output) => output.show_pairing_code(),
                    None => match tls_identity(&identity)
                        .and_then(|identity| radio::pair(identity, &capture))
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
        #[cfg(any(feature = "ble-link", feature = "wifi-map"))]
        tiles.select(tile, now);
        #[cfg(all(feature = "wifi-map", not(feature = "ble-link")))]
        let (view, phone_tile) = (View::default(), None);
        #[cfg(feature = "ble-link")]
        let (view, phone_tile) = {
            let mut phone_tile = None;
            let view = match radio.as_mut() {
                Some(output) => {
                    let samples_ready = capture.take_ready();
                    if output.take_ready() || samples_ready || now >= next_radio {
                        phone_tile = output.service(now, tiles.phone_request());
                        next_radio = now + ACTIVE_POLL;
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
                None if pairing_failed => View {
                    link: LinkStatus::Unavailable,
                    key: None,
                },
                None => View::default(),
            };
            let previous = std::mem::replace(&mut shared.lock().unwrap().view, view);
            if view != previous {
                notify(Update::Links);
            }
            (view, phone_tile)
        };
        #[cfg(feature = "cellular-uplink")]
        if let Some(uplink) = &mut uplink {
            uplink.service(
                sample.map(Into::into),
                now,
                || tls_identity(&identity),
                crate::wire::cellular::frame,
            );
        }
        #[cfg(any(feature = "ble-link", feature = "wifi-map"))]
        if let Some(Response::Tile(tile)) = tiles.service(
            #[cfg(feature = "wifi-map")]
            || tls_identity(&identity),
            view.link == LinkStatus::Connected,
            phone_tile,
            now,
        ) {
            let mut inbox = shared.lock().unwrap();
            if inbox.tile_request == Some(tile.key) {
                inbox.tile = Some(tile);
                drop(inbox);
                notify(Update::Links);
            }
        }
        let now = Instant::now();
        let wait = telemetry.deadline().saturating_duration_since(now);
        #[cfg(feature = "ble-link")]
        let wait = if radio.is_some() {
            wait.min(ACTIVE_POLL)
        } else {
            wait
        };
        #[cfg(any(feature = "ble-link", feature = "wifi-map"))]
        let wait = if tiles.busy() {
            wait.min(ACTIVE_POLL)
        } else {
            wait
        };
        #[cfg(feature = "cellular-uplink")]
        let wait = uplink
            .as_ref()
            .and_then(|uplink| uplink.wake_in(now))
            .map_or(wait, |link_wait| wait.min(link_wait));
        thread::park_timeout(wait);
    }
    #[cfg(feature = "ble-link")]
    drop(radio);
    let sensors = telemetry.stop();
    #[cfg(feature = "cellular-uplink")]
    if let Some(uplink) = uplink {
        return sensors.and(uplink.stop());
    }
    sensors
}
