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

    /// Every pending operation contributes to the communications owner's one
    /// wait. A retired viewport's dial still has to be reaped before reuse.
    pub fn next_service(&self, poll: Instant, phone_live: bool) -> Option<Instant> {
        if self.dial.is_some() {
            return Some(match self.active.as_ref() {
                Some(Transfer::Relay(deadline, _)) => poll.min(*deadline),
                _ => poll,
            });
        }
        self.key?;
        match self.active.as_ref() {
            Some(Transfer::Phone(deadline)) => Some(*deadline),
            Some(Transfer::Relay(deadline, _)) => Some(poll.min(*deadline)),
            None if (phone_live && !self.try_ip)
                || (DIRECT_RELAY_AVAILABLE && self.relay.is_some()) =>
            {
                Some(self.next.max(self.throttle))
            }
            None => None,
        }
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
                if dial.poll().is_some() {
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

    fn poll(&mut self) -> Option<io::Result<TcpStream>> {
        if self.0.as_ref().is_some_and(|task| task.is_finished()) {
            Some(
                self.0
                    .take()
                    .unwrap()
                    .join()
                    .unwrap_or_else(|_| Err(io::Error::other("tile dial panicked"))),
            )
        } else {
            None
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
                if let Some(result) = dial.as_mut().unwrap().poll() {
                    *dial = None;
                    let socket = result?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    fn key(x: u32) -> Key {
        Key {
            zoom: Zoom::default(),
            x,
            y: 1,
        }
    }

    fn relay() -> Endpoint {
        Endpoint {
            address: std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 9),
            pin: [1; 32],
        }
    }

    fn failed_dial() -> Dial {
        let task = thread::spawn(|| Err(io::ErrorKind::ConnectionRefused.into()));
        let deadline = Instant::now() + Duration::from_secs(1);
        while !task.is_finished() {
            assert!(Instant::now() < deadline);
            thread::yield_now();
        }
        Dial(Some(task))
    }

    #[test]
    fn failed_dial_retires_its_slot_and_owner_wakes_for_the_same_tile() {
        if !DIRECT_RELAY_AVAILABLE {
            return;
        }
        let (sent, received) = mpsc::channel();
        let owner = thread::spawn(move || {
            let dial = failed_dial();
            // Keep the real 30 s retry policy; choose a past event instant so
            // the real owner wait expires soon without a synthetic unpark.
            let failure_at = Instant::now() - RETRY + Duration::from_millis(30);
            let mut tiles = Tiles::new(Some(relay()), failure_at);
            tiles.select(Some(key(1)), failure_at);
            tiles.dial = Some(dial);
            tiles.active = Some(Transfer::Relay(failure_at + IP_DEADLINE, Relay::Dial));
            assert!(tiles
                .service(|| unreachable!(), false, None, failure_at)
                .is_none());
            assert!(tiles.dial.is_none());
            assert!(tiles.active.is_none());
            assert_eq!(
                tiles.next_service(Instant::now() + super::super::ACTIVE_POLL, false),
                Some(failure_at + RETRY)
            );
            loop {
                super::super::wait_for_work(None, &tiles, false);
                if Instant::now() >= failure_at + RETRY {
                    break;
                }
            }
            sent.send(()).unwrap();
        });
        let result = received.recv_timeout(Duration::from_secs(1));
        owner.thread().unpark(); // Release a broken implementation for a useful failure.
        owner.join().unwrap();
        assert!(
            result.is_ok(),
            "tile retry must wake an otherwise idle owner"
        );
    }

    #[test]
    fn replacing_a_viewport_keeps_its_unfinished_dial_in_the_wait_set() {
        if !DIRECT_RELAY_AVAILABLE {
            return;
        }
        let now = Instant::now();
        let (complete, wait) = mpsc::channel();
        let mut tiles = Tiles::new(Some(relay()), now);
        tiles.select(Some(key(1)), now);
        tiles.dial = Some(Dial(Some(thread::spawn(move || {
            wait.recv().unwrap();
            Err(io::ErrorKind::ConnectionRefused.into())
        }))));
        tiles.active = Some(Transfer::Relay(now + IP_DEADLINE, Relay::Dial));
        tiles.select(Some(key(2)), now);
        let poll = now + Duration::from_millis(15);
        assert_eq!(tiles.next_service(poll, false), Some(poll));
        complete.send(()).unwrap();
        // Drop still joins the bounded in-flight connect before owner exit.
    }

    #[test]
    fn unavailable_transport_sleeps_and_phone_failure_retains_retry_deadline() {
        let now = Instant::now();
        let mut tiles = Tiles::new(None, now);
        tiles.select(Some(key(1)), now);
        assert_eq!(tiles.next_service(now, false), None);
        tiles.service(|| unreachable!(), true, None, now);
        assert_eq!(tiles.next_service(now, true), Some(now + BLE_DEADLINE));
        tiles.service(
            || unreachable!(),
            true,
            Some(Response::Unavailable(key(1))),
            now,
        );
        assert_eq!(tiles.next_service(now, true), Some(now + RETRY));
        tiles.next = now;
        assert_eq!(tiles.next_service(now, true), Some(now + REQUEST_INTERVAL));
    }
}
