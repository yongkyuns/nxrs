//! Route one visible tile request over the paired phone or a pinned IP relay.
use super::Endpoint;
use crate::map::{Key, Response};
#[cfg(feature = "wifi-map")]
use crate::map::{REQUEST_BYTES, Receiver};
#[cfg(feature = "wifi-map")]
use connectivity::Dial;
use std::{
    io,
    time::{Duration, Instant},
};
#[cfg(feature = "wifi-map")]
use std::{io::Write, thread};

const BLE_DEADLINE: Duration = Duration::from_secs(25);
#[cfg(feature = "wifi-map")]
const IP_DEADLINE: Duration = Duration::from_secs(12);
const RETRY: Duration = Duration::from_secs(30);
const REQUEST_INTERVAL: Duration = Duration::from_secs(5);

pub struct Tiles<'a> {
    relay: Option<Endpoint>,
    #[cfg(feature = "wifi-map")]
    dial: Option<Dial>,
    key: Option<Key>,
    active: Option<Transfer<'a>>,
    #[cfg(feature = "wifi-map")]
    receiver: Receiver,
    next: Instant,
    throttle: Instant,
    try_ip: bool,
}

enum Transfer<'a> {
    Phone(Instant),
    #[cfg(feature = "wifi-map")]
    Relay(Instant, Relay<'a>),
    #[cfg(not(feature = "wifi-map"))]
    _IpUnavailable(std::marker::PhantomData<&'a ()>),
}

impl<'a> Tiles<'a> {
    pub fn new(relay: Option<Endpoint>, now: Instant) -> Self {
        Self {
            relay,
            #[cfg(feature = "wifi-map")]
            dial: None,
            key: None,
            active: None,
            #[cfg(feature = "wifi-map")]
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
            #[cfg(feature = "wifi-map")]
            {
                self.receiver = Receiver::default();
            }
            self.try_ip = false;
            self.next = now;
        }
    }

    #[cfg(feature = "ble-link")]
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
        #[cfg(feature = "wifi-map")] identity: impl FnOnce() -> io::Result<&'a tls::Identity>,
        phone_live: bool,
        phone: Option<Response>,
        now: Instant,
    ) -> Option<Response> {
        #[cfg(feature = "wifi-map")]
        if !matches!(self.active, Some(Transfer::Relay(_, Relay::Dial))) {
            if let Some(dial) = &mut self.dial {
                if !matches!(dial.poll(), Ok(None)) {
                    self.dial = None;
                }
            }
        }
        let key = self.key?;
        let result: io::Result<Option<Response>> = match self.active.as_mut() {
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
            #[cfg(feature = "wifi-map")]
            Some(Transfer::Relay(deadline, relay)) if now < *deadline => relay.poll(
                key,
                self.relay.unwrap(),
                &mut self.dial,
                &mut self.receiver,
                identity,
            ),
            #[cfg(feature = "wifi-map")]
            Some(Transfer::Relay(..)) => Err(io::ErrorKind::TimedOut.into()),
            #[cfg(not(feature = "wifi-map"))]
            Some(Transfer::_IpUnavailable(_)) => return None,
            None => {
                if now < self.next || now < self.throttle {
                    return None;
                }
                if phone_live && !self.try_ip {
                    self.throttle = now + REQUEST_INTERVAL;
                    self.active = Some(Transfer::Phone(now + BLE_DEADLINE));
                    return None;
                }
                #[cfg(feature = "wifi-map")]
                if let Some(endpoint) = self.relay.filter(|_| self.dial.is_none()) {
                    let worker = thread::Builder::new()
                        .name("tracker-tile-dial".into())
                        .stack_size(8 * 1024);
                    if let Ok(dial) = Dial::start(worker, endpoint, Duration::from_secs(3)) {
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
        #[cfg(feature = "wifi-map")]
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
                #[cfg(feature = "wifi-map")]
                {
                    self.receiver = Receiver::default();
                }
                self.next = if self.try_ip { now } else { now + RETRY };
                None
            }
        }
    }
}

#[cfg(feature = "wifi-map")]
enum Relay<'a> {
    Dial,
    Request {
        connection: tls::Connection<'a>,
        request: [u8; REQUEST_BYTES],
        written: usize,
        ready: bool,
    },
}

#[cfg(feature = "wifi-map")]
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
                    let connection = endpoint
                        .secure(identity()?, socket, IP_DEADLINE)
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
