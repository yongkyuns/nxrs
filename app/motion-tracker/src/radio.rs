use crate::{
    export::{Capture, MAX_BYTES, OVERFLOW_FRAME, REQUEST},
    text::Text,
    tracker::Snapshot,
    wire,
};
use ble::{
    peripheral::{self, Link, Listener, Service},
    Uuid,
};
use std::io::{self, Read, Write};
use std::time::{Duration, Instant};
use telemetry_service::stream::Publisher;

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
    let listener = peripheral::open_with_notifications(service(0), service(0x10))
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
    listener: L,
    identity: &'a tls::Identity,
    key: [u8; 32],
    capture: &'a Capture,
    request: [u8; REQUEST.len()],
    request_len: usize,
    export: [u8; MAX_BYTES],
    export_len: usize,
    sent: usize,
    next_export: Instant,
    connection: Option<tls::Connection<'a, Bearer<L::Transport>>>,
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
            request: [0; REQUEST.len()],
            request_len: 0,
            export: [0; MAX_BYTES],
            export_len: 0,
            sent: 0,
            next_export: now,
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

    pub fn service(&mut self, now: Instant) {
        if let Err(error) = self.poll(now) {
            eprintln!("motion-tracker BLE session ended: {error}");
            self.connection = None;
            self.capture.disable();
            self.request_len = 0;
            self.export_len = 0;
            self.sent = 0;
            self.live = false;
            self.drained = false;
            self.publication = Publisher::default();
        }
    }

    fn poll(&mut self, now: Instant) -> io::Result<()> {
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
                if notifications && self.request_len < REQUEST.len() && self.publication.is_idle() {
                    match conn.read(&mut self.request[self.request_len..]) {
                        Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
                        Ok(count) => {
                            self.request_len += count;
                            if self.request[..self.request_len] != REQUEST[..self.request_len] {
                                return Err(io::ErrorKind::InvalidData.into());
                            }
                            if self.request_len == REQUEST.len() {
                                self.capture.enable()?;
                                self.next_export = now + Duration::from_millis(40);
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
                    return Ok(());
                }
                if !self.publication.is_idle() {
                    self.drained =
                        self.publication
                            .service(conn, now, self.expires, wire::frame)?;
                    return Ok(());
                }
                self.drained = match conn.flush() {
                    Ok(()) => true,
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => false,
                    Err(error) => return Err(error),
                };
                if notifications && self.request_len == REQUEST.len() && now >= self.next_export {
                    self.export_len = self.capture.take(&mut self.export);
                    self.next_export = if self.export_len == OVERFLOW_FRAME.len() {
                        self.expires
                    } else {
                        now + Duration::from_millis(40)
                    };
                    if self.export_len != 0 {
                        Self::write_export(conn, &self.export[..self.export_len], &mut self.sent)?;
                        if self.sent == self.export_len {
                            self.sent = 0;
                            self.export_len = 0;
                        }
                    }
                }
            }
        }
        Ok(())
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

    pub fn offer(&mut self, snapshot: &Snapshot, now: Instant) {
        if self.live
            && (self.drained || self.listener.notifying())
            && self.publication.is_idle()
            && now >= self.next_frame
        {
            self.publication.offer(*snapshot);
            self.next_frame = now + Duration::from_secs(1);
        }
    }
}
impl<L: Listener> Drop for Output<'_, L> {
    fn drop(&mut self) {
        self.connection = None;
        self.capture.disable();
        self.key.fill(0);
    }
}
