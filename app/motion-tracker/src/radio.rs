use crate::{text::Text, tracker::Snapshot, wire};
use ble::{
    peripheral::{self, Link, Listener},
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

pub fn pair(identity: &tls::Identity) -> io::Result<Output<'_, impl Listener>> {
    let listener = peripheral::open(uuid(0), uuid(1), uuid(2))
        .map_err(|error| io::Error::other(format!("BLE: {error:?}")))?;
    Output::start(listener, identity)
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
    connection: Option<tls::Connection<'a, Bearer<L::Transport>>>,
    publication: Publisher<Snapshot, Text<768>>,
    live: bool,
    paired: bool,
    drained: bool,
    next_frame: Instant,
    expires: Instant,
}

impl<'a, L: Listener> Output<'a, L> {
    pub fn start(listener: L, identity: &'a tls::Identity) -> io::Result<Self> {
        let key = identity
            .pairing_key()
            .map_err(|error| io::Error::other(format!("Pairing key: {error:?}")))?;
        let now = Instant::now();
        Ok(Self {
            listener,
            identity,
            key,
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

    pub fn expired(&self, now: Instant) -> bool {
        now >= self.expires
    }

    pub fn service(&mut self, now: Instant) {
        if self.poll(now).is_err() {
            self.connection = None;
            self.live = false;
            self.drained = false;
            self.publication = Publisher::default();
        }
    }

    fn poll(&mut self, now: Instant) -> io::Result<()> {
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
                self.drained = if self.publication.is_idle() {
                    match conn.flush() {
                        Ok(()) => true,
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => false,
                        Err(error) => return Err(error),
                    }
                } else {
                    self.publication
                        .service(conn, now, self.expires, wire::frame)?
                };
            }
        }
        Ok(())
    }

    pub fn offer(&mut self, snapshot: &Snapshot, now: Instant) {
        if self.live && self.drained && self.publication.is_idle() && now >= self.next_frame {
            self.publication.offer(*snapshot);
            self.next_frame = now + Duration::from_secs(1);
        }
    }
}
impl<L: Listener> Drop for Output<'_, L> {
    fn drop(&mut self) {
        self.connection = None;
        self.key.fill(0);
    }
}
