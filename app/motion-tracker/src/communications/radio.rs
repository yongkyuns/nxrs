use super::{
    export::{Capture, Version, MAX_BYTES, REQUEST_LEN, REQUEST_PREFIX},
    publication::Publisher,
    wire,
};
use crate::{
    map::{Key, Receiver, Response},
    text::Text,
    tracker::Snapshot,
};
use ble::{
    peripheral::{self, Link, Listener, Service},
    Uuid,
};
use std::io::{self, Read, Write};
use std::time::{Duration, Instant};

fn uuid(last: u8) -> Uuid {
    Uuid([
        0x3c, 0xb1, 0xde, 0x6e, 0xa9, 0x55, 0x49, 0xb7, 0xbe, 0x4f, 0xc4, 0x57, 0x2d, 0x3f, 0x34,
        last,
    ])
}

pub fn pair<'a>(
    identity: &'a tls::Identity,
    capture: &'a Capture,
) -> io::Result<Output<'a, impl Listener>> {
    let service = |base| Service {
        uuid: uuid(base),
        ingress: uuid(base + 1),
        egress: uuid(base + 2),
    };
    let listener = peripheral::open_with_notifications(service(0), service(0x20))
        .map_err(|error| io::Error::other(format!("BLE: {error:?}")))?;
    Output::start(listener, identity, capture)
}

struct Bearer<T>(T);
impl<T: Link> Read for Bearer<T> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.0.read(buf)
    }
}
impl<T: Link> Write for Bearer<T> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.0.flush()
    }
}
impl<T: Link> tls::Stream for Bearer<T> {
    fn prepare(&mut self) -> io::Result<()> {
        Ok(())
    }
    fn abort(&mut self) {
        self.0.abort();
    }
}

pub struct Output<'a, L: Listener> {
    // Fields leave scope in declaration order. Retire TLS before its listener.
    connection: Option<tls::Connection<'a, Bearer<L::Transport>>>,
    listener: L,
    identity: &'a tls::Identity,
    key: [u8; 32],
    capture: &'a Capture,
    tile: Receiver,
    advertised_tile: Option<Key>,
    request: [u8; REQUEST_LEN],
    request_len: usize,
    export: [u8; MAX_BYTES],
    export_len: usize,
    sent: usize,
    next_export: Option<(Version, Instant)>,
    last_export_service: Option<Instant>,
    max_service_gap: Duration,
    publication: Publisher<Snapshot, Text<768>>,
    live: bool,
    paired: bool,
    drained: bool,
    next_frame: Instant,
    expires: Instant,
}

impl<'a, L: Listener> Output<'a, L> {
    pub fn start(
        listener: L,
        identity: &'a tls::Identity,
        capture: &'a Capture,
    ) -> io::Result<Self> {
        let key = identity
            .pairing_key()
            .map_err(|error| io::Error::other(format!("Pairing key: {error:?}")))?;
        let now = Instant::now();
        Ok(Self {
            listener,
            identity,
            key,
            capture,
            tile: Receiver::default(),
            advertised_tile: None,
            request: [0; REQUEST_LEN],
            request_len: 0,
            export: [0; MAX_BYTES],
            export_len: 0,
            sent: 0,
            next_export: None,
            last_export_service: None,
            max_service_gap: Duration::ZERO,
            connection: None,
            publication: Publisher::default(),
            live: false,
            paired: false,
            drained: false,
            next_frame: now,
            expires: now + Duration::from_secs(3600),
        })
    }
    pub fn key(&self) -> &[u8; 32] {
        &self.key
    }
    pub fn live(&self) -> bool {
        self.live
    }
    pub fn paired(&self) -> bool {
        self.paired
    }

    pub fn show_pairing_code(&mut self) {
        if !self.live {
            self.paired = false;
        }
    }

    pub fn expired(&self, now: Instant) -> bool {
        now >= self.expires
    }

    pub fn active(&self) -> bool {
        self.connection.is_some()
    }

    pub fn next_service(&self, active_at: Instant) -> Instant {
        let wake = if self.active() {
            active_at
        } else {
            self.listener.idle_retry_at().unwrap_or(self.expires)
        };
        wake.min(self.expires)
    }

    pub fn take_ready(&self) -> bool {
        self.listener.take_ready()
    }

    pub fn service(&mut self, now: Instant, wanted: Option<Key>) -> Option<Response> {
        self.tile.retain(wanted);
        if let Some(last) = self.last_export_service.as_mut() {
            self.max_service_gap = self.max_service_gap.max(now.duration_since(*last));
            *last = now;
        }
        let error = match self.poll(now, wanted) {
            Ok(response) => return response,
            Err(error) => error,
        };
        self.connection = None;
        let high_water = self.capture.finish();
        log::warn!("motion-tracker BLE session ended: {error}");
        if self.last_export_service.take().is_some() {
            log::info!(
                "motion-tracker BLE export: high_water={high_water} samples max_service_gap={} us",
                self.max_service_gap.as_micros(),
            );
        }
        self.tile = Receiver::default();
        self.advertised_tile = None;
        self.request_len = 0;
        self.export_len = 0;
        self.next_export = None;
        self.sent = 0;
        self.live = false;
        self.drained = false;
        self.publication = Publisher::default();
        None
    }

    fn poll(&mut self, now: Instant, wanted: Option<Key>) -> io::Result<Option<Response>> {
        let mut response = None;
        self.listener.service()?;
        if self.connection.is_none() {
            if let Some(stream) = self
                .listener
                .accept()
                .map_err(|_| io::ErrorKind::NotConnected)?
            {
                let conn = self
                    .identity
                    .accept_pairing(Bearer(stream), &self.key)
                    .map_err(|_| io::ErrorKind::PermissionDenied)?;
                self.connection = Some(conn);
            }
        }
        if let Some(conn) = self.connection.as_mut() {
            self.live = conn.poll().map_err(|_| io::ErrorKind::PermissionDenied)?;
            if self.live {
                self.paired = true;
                let notifications = self.listener.notifying();
                if notifications && self.request_len < REQUEST_LEN && self.publication.is_idle() {
                    match conn.read(&mut self.request[self.request_len..]) {
                        Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
                        Ok(count) => {
                            self.request_len += count;
                            let prefix_len = self.request_len.min(REQUEST_PREFIX.len());
                            if self.request[..prefix_len] != REQUEST_PREFIX[..prefix_len] {
                                return Err(io::ErrorKind::InvalidData.into());
                            }
                            if self.request_len == REQUEST_LEN {
                                let version = Version::from_request(self.request[REQUEST_LEN - 1])
                                    .ok_or(io::ErrorKind::InvalidData)?;
                                self.capture.enable()?;
                                self.last_export_service = Some(now);
                                self.max_service_gap = Duration::ZERO;
                                self.next_export = Some((version, now + version.batch_interval()));
                            }
                        }
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                        Err(error) => return Err(error),
                    }
                }
                if self.export_len != 0 {
                    Self::write_export(conn, &self.export[..self.export_len], &mut self.sent)?;
                    if self.sent == self.export_len {
                        self.sent = 0;
                        self.export_len = 0;
                    }
                    return Ok(None);
                }
                let tile_ready = !notifications || self.request_len == REQUEST_LEN;
                let requested_tile = wanted.filter(|_| tile_ready);
                if !self.publication.is_idle() {
                    self.drained = if notifications {
                        self.publication
                            .enqueue(conn, now, self.expires, |snapshot| {
                                let frame = wire::export_frame(
                                    snapshot,
                                    self.capture.scales(),
                                    requested_tile,
                                )?;
                                self.advertised_tile = requested_tile;
                                Ok(frame)
                            })?
                    } else {
                        self.publication
                            .service(conn, now, self.expires, |snapshot| {
                                let frame = wire::ble_frame(snapshot, requested_tile)?;
                                self.advertised_tile = requested_tile;
                                Ok(frame)
                            })?
                    };
                    return Ok(None);
                }
                if !notifications {
                    self.drained = match conn.flush() {
                        Ok(()) => true,
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => false,
                        Err(error) => return Err(error),
                    };
                }
                // Read a bounded portion per turn, then service the sensor batch.
                if tile_ready && (notifications || self.drained) {
                    response = self.tile.poll(conn, wanted)?;
                }
                if let Some((version, deadline)) = self.next_export.filter(|_| notifications) {
                    if let Some(batch) =
                        self.capture
                            .take(&mut self.export, now >= deadline, version)
                    {
                        self.export_len = batch.len;
                        self.next_export =
                            (!batch.terminal).then(|| (version, now + version.batch_interval()));
                        if batch.terminal {
                            self.capture.finish();
                        }
                        Self::write_export(conn, &self.export[..self.export_len], &mut self.sent)?;
                        if self.sent == self.export_len {
                            self.sent = 0;
                            self.export_len = 0;
                        }
                    }
                }
            }
        }
        Ok(response)
    }

    fn write_export(conn: &mut impl Write, bytes: &[u8], sent: &mut usize) -> io::Result<()> {
        match conn.write(&bytes[*sent..]) {
            Ok(0) => Err(io::ErrorKind::WriteZero.into()),
            Ok(count) => {
                *sent += count;
                Ok(())
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => Ok(()),
            Err(error) => Err(error),
        }
    }

    pub fn offer(&mut self, snapshot: &Snapshot, now: Instant, wanted: Option<Key>) {
        let new_tile = wanted.is_some()
            && wanted != self.advertised_tile
            && (!self.listener.notifying() || self.request_len == REQUEST_LEN);
        if self.live
            && (self.drained || self.listener.notifying())
            && self.publication.is_idle()
            && (now >= self.next_frame || new_tile)
        {
            self.publication.offer(*snapshot);
            self.next_frame = now + Duration::from_secs(1);
        }
    }
}
impl<L: Listener> Drop for Output<'_, L> {
    fn drop(&mut self) {
        self.capture.finish();
        self.key.fill(0);
    }
}
