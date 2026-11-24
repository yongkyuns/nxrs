//! One task steps both TLS sessions under the SDK's single identity lease.
mod export;
mod publication;
mod radio;
mod tiles;
mod uplink;

pub(super) use export::Capture;
pub(super) use uplink::{Config as CellularConfig, Endpoint};

use crate::{
    map::{Key, Response, Tile},
    tracker::Snapshot,
};
use publication::Publisher;
use std::cell::OnceCell;
use std::io;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

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
    latest: Option<Snapshot>,
    new_sample: bool,
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
        capture: Arc<Capture>,
        notify: impl FnMut() + Send + 'static,
    ) -> io::Result<Self> {
        let shared = Arc::new(Mutex::new(Shared::default()));
        let incoming = shared.clone();
        let task = thread::Builder::new()
            .name("tracker-links".into())
            .stack_size(24 * 1024)
            .spawn(move || run(&incoming, pair, cellular, tile_relay, &capture, notify))?;
        Ok(Self {
            shared,
            task: Some(task),
        })
    }

    pub fn pair(&self, command: Pairing) {
        self.shared.lock().unwrap().pairing = Some(command);
        self.task.as_ref().unwrap().thread().unpark();
    }

    pub fn offer(&self, sample: Snapshot) {
        if let Ok(mut shared) = self.shared.try_lock() {
            shared.latest = Some(sample);
            shared.new_sample = true;
            self.task.as_ref().unwrap().thread().unpark();
        }
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
    capture: &Capture,
    mut notify: impl FnMut(),
) -> io::Result<()> {
    capture.register_owner();
    let collector = cellular.as_ref().and_then(|config| config.collector);
    let mut modem = cellular.map(uplink::Modem::start).transpose()?;
    let identity = OnceCell::new();
    let mut radio: Option<radio::Output<'_, _>> = None;
    let mut tiles = tiles::Tiles::new(tile_relay, Instant::now());
    let mut next_radio = Instant::now();
    let mut upload = None;
    let mut retry = uplink::Retry::new();
    let mut retry_pending = false;
    let mut link_generation = 0;
    let mut pairing_failed = false;
    let mut publication = Publisher::default();
    let mut command = pair.then_some(Pairing::Start);

    loop {
        let (sample, new_sample, pair, tile) = {
            let mut inbox = shared.lock().unwrap();
            if inbox.stopping {
                break;
            }
            let tile = inbox
                .tile_request
                .filter(|key| inbox.tile.as_ref().is_none_or(|tile| tile.key != *key));
            (
                inbox.latest,
                std::mem::take(&mut inbox.new_sample),
                inbox.pairing.take().or(command.take()),
                tile,
            )
        };
        let now = Instant::now();
        if radio.as_ref().is_some_and(|output| output.expired(now)) {
            radio = None;
        }
        if let Some(pair) = pair {
            pairing_failed = false;
            match pair {
                Pairing::Cancel => radio = None,
                Pairing::Start => match radio.as_mut() {
                    Some(output) => output.show_pairing_code(),
                    None => match tls_identity(&identity)
                        .and_then(|identity| radio::pair(identity, capture))
                    {
                        Ok(output) => radio = Some(output),
                        Err(error) => {
                            eprintln!("motion-tracker pairing: {error}");
                            pairing_failed = true;
                        }
                    },
                },
            }
        }
        tiles.select(tile, now);
        let mut phone_tile = None;
        let view = match radio.as_mut() {
            Some(output) => {
                let samples_ready = capture.take_ready();
                if output.take_ready() || samples_ready || now >= next_radio {
                    phone_tile = output.service(now, tiles.phone_request());
                    next_radio = now + ACTIVE_POLL;
                }
                if let Some(snapshot) = &sample {
                    output.offer(snapshot, new_sample, now, tiles.phone_request());
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
            notify();
        }
        if let (Some(collector), Some(modem)) = (collector, modem.as_ref()) {
            if let Some(sample) = sample.filter(|_| new_sample) {
                publication.offer(sample);
            }
            let (generation, socket) = modem.receive();
            if generation != link_generation {
                link_generation = generation;
                upload = None;
                publication.reset_stream();
                retry = uplink::Retry::new();
                retry_pending = false;
            }
            let progress = if let Some(socket) = socket {
                tls_identity(&identity)
                    .and_then(|identity| uplink::Upload::open(identity, socket, collector))
                    .map(|connection| {
                        upload = Some(connection);
                        publication.reset_stream();
                        false
                    })
            } else {
                upload.as_mut().map_or(Ok(false), |connection| {
                    connection.service(&mut publication, now)
                })
            };
            match progress {
                Ok(true) => retry = uplink::Retry::new(),
                Ok(false) => {}
                Err(error) => {
                    eprintln!("motion-tracker collector retry: {error}");
                    upload = None;
                    publication.reset_stream();
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
        if radio.is_some() || upload.is_some() || tiles.busy() {
            thread::park_timeout(ACTIVE_POLL);
        } else if retry_pending {
            thread::park_timeout(retry.next.saturating_duration_since(Instant::now()));
        } else {
            thread::park();
        }
    }
    let _ = upload.take();
    drop(radio);
    modem.as_mut().map_or(Ok(()), uplink::Modem::stop)
}
