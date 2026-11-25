//! Route one visible tile request over the paired phone or a pinned IP relay.
mod wire;

use wire::REQUEST_BYTES;
pub use wire::{Key, Receiver, Response, Tile, Zoom, SIDE};

use super::Endpoint;
use std::{
    io::{self, Write},
    net::{SocketAddr, TcpStream},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

pub const DIRECT_RELAY_AVAILABLE: bool = !cellular::LINK_AVAILABLE;

const BLE_DEADLINE: Duration = Duration::from_secs(25);
const IP_DEADLINE: Duration = Duration::from_secs(12);
const RETRY: Duration = Duration::from_secs(30);
const REQUEST_INTERVAL: Duration = Duration::from_secs(5);

pub struct Tiles<'a> {
    relay: Option<Endpoint>,
    dial: Option<Dial>,
    key: Option<Key>,
    active: Option<Transfer<'a>>,
    receiver: Receiver,
    next: Instant,
    throttle: Instant,
    try_ip: bool,
}

enum Transfer<'a> {
    Phone(Instant),
    Relay(Instant, Relay<'a>),
}

impl<'a> Tiles<'a> {
    pub fn new(relay: Option<Endpoint>, now: Instant) -> Self {
        Self {
            relay,
            dial: None,
            key: None,
            active: None,
            receiver: Receiver::default(),
            next: now,
            throttle: now,
            try_ip: false,
        }
    }

    pub fn select(&mut self, key: Option<Key>, now: Instant) {
        if key != self.key {
            self.key = key;
            self.active = None;
            self.receiver = Receiver::default();
            self.try_ip = false;
            self.next = now;
        }
    }

    pub fn phone_request(&self) -> Option<Key> {
        if matches!(self.active, Some(Transfer::Phone(_))) {
            self.key
        } else {
            None
        }
    }

    pub fn busy(&self) -> bool {
        self.active.is_some()
    }

    pub fn service(
        &mut self,
        identity: impl FnOnce() -> io::Result<&'a tls::Identity>,
        phone_live: bool,
        phone: Option<Response>,
        now: Instant,
    ) -> Option<Response> {
        if DIRECT_RELAY_AVAILABLE && !matches!(self.active, Some(Transfer::Relay(_, Relay::Dial))) {
            if let Some(dial) = &mut self.dial {
                if !matches!(dial.poll(), Ok(None)) {
                    self.dial = None;
                }
            }
        }
        let key = self.key?;
        let result = match self.active.as_mut() {
            Some(Transfer::Phone(deadline)) => match phone {
                Some(Response::Tile(tile)) if tile.key == key => {
                    self.active = None;
                    self.throttle = now;
                    return Some(Response::Tile(tile));
                }
                Some(Response::Unavailable(failed)) if failed == key => {
                    Err(io::ErrorKind::NotFound.into())
                }
                _ if !phone_live || now >= *deadline => Err(io::ErrorKind::TimedOut.into()),
                _ => return None,
            },
            Some(Transfer::Relay(deadline, relay)) if DIRECT_RELAY_AVAILABLE && now < *deadline => {
                relay.poll(
                    key,
                    self.relay.unwrap(),
                    &mut self.dial,
                    &mut self.receiver,
                    identity,
                )
            }
            Some(Transfer::Relay(..)) => Err(io::ErrorKind::TimedOut.into()),
            None => {
                if now < self.next || now < self.throttle {
                    return None;
                }
                if phone_live && !self.try_ip {
                    self.throttle = now + REQUEST_INTERVAL;
                    self.active = Some(Transfer::Phone(now + BLE_DEADLINE));
                } else if let Some(endpoint) = self
                    .relay
                    .filter(|_| DIRECT_RELAY_AVAILABLE && self.dial.is_none())
                {
                    if let Ok(dial) = Dial::start(endpoint) {
                        self.throttle = now + REQUEST_INTERVAL;
                        self.dial = Some(dial);
                        self.active = Some(Transfer::Relay(now + IP_DEADLINE, Relay::Dial));
                    } else {
                        self.next = now + RETRY;
                    }
                }
                return None;
            }
        };
        if let Err(error) = &result {
            if matches!(self.active, Some(Transfer::Relay(..))) {
                eprintln!("motion-tracker map relay: {error}");
            }
        }
        match result {
            Ok(None) => None,
            Ok(Some(response @ Response::Tile(_))) => {
                self.active = None;
                self.throttle = now;
                Some(response)
            }
            Ok(Some(Response::Unavailable(_))) | Err(_) => {
                self.try_ip =
                    matches!(self.active, Some(Transfer::Phone(_))) && self.relay.is_some();
                self.active = None;
                self.receiver = Receiver::default();
                self.next = if self.try_ip { now } else { now + RETRY };
                None
            }
        }
    }
}

// A kernel TCP connect may wait for SYN retransmission. Keep it off the BLE,
// collector and display loops. During normal operation joins occur after the
// attempt finishes, including when the viewport has changed.
struct Dial(Option<JoinHandle<io::Result<TcpStream>>>);

impl Dial {
    fn start(endpoint: Endpoint) -> io::Result<Self> {
        if !tls::Identity::SUPPORTS_PINNED_CLIENT {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "map relay TLS is unavailable",
            ));
        }
        thread::Builder::new()
            .name("tracker-tile-dial".into())
            .stack_size(8 * 1024)
            .spawn(move || {
                TcpStream::connect_timeout(
                    &SocketAddr::V4(endpoint.address),
                    Duration::from_secs(3),
                )
            })
            .map(|thread| Self(Some(thread)))
    }

    fn poll(&mut self) -> io::Result<Option<TcpStream>> {
        if self.0.as_ref().is_some_and(|task| task.is_finished()) {
            self.0
                .take()
                .unwrap()
                .join()
                .map_err(|_| io::Error::other("tile dial panicked"))?
                .map(Some)
        } else {
            Ok(None)
        }
    }
}

impl Drop for Dial {
    fn drop(&mut self) {
        if let Some(task) = self.0.take() {
            let _ = task.join();
        }
    }
}

enum Relay<'a> {
    Dial,
    Request {
        connection: tls::Connection<'a>,
        request: [u8; REQUEST_BYTES],
        written: usize,
        ready: bool,
    },
}

impl<'a> Relay<'a> {
    fn poll(
        &mut self,
        key: Key,
        endpoint: Endpoint,
        dial: &mut Option<Dial>,
        receiver: &mut Receiver,
        identity: impl FnOnce() -> io::Result<&'a tls::Identity>,
    ) -> io::Result<Option<Response>> {
        match self {
            Self::Dial => {
                if let Some(socket) = dial.as_mut().unwrap().poll()? {
                    *dial = None;
                    let connection = identity()?
                        .connect_pinned(socket, endpoint.pin, IP_DEADLINE)
                        .map_err(|_| io::ErrorKind::PermissionDenied)?;
                    *self = Self::Request {
                        connection,
                        request: key.request(),
                        written: 0,
                        ready: false,
                    };
                }
            }
            Self::Request {
                connection,
                request,
                written,
                ready,
            } => {
                if !*ready {
                    *ready = connection
                        .poll()
                        .map_err(|_| io::ErrorKind::PermissionDenied)?;
                    if !*ready {
                        return Ok(None);
                    }
                }
                if *written < request.len() {
                    match connection.write(&request[*written..]) {
                        Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
                        Ok(count) => *written += count,
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                        Err(error) => return Err(error),
                    }
                } else {
                    match connection.flush() {
                        Ok(()) => return receiver.poll(connection, Some(key)),
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                        Err(error) => return Err(error),
                    }
                }
            }
        }
        Ok(None)
    }
}
