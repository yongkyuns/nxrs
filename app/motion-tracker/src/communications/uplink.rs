//! Collector TCP and TLS delivery over an available cellular link.
use super::publication::Publisher;
use super::{
    wire::{self, Frame},
    Snapshot,
};
use std::io::{self, Read};
use std::net::{SocketAddr, SocketAddrV4, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const SESSION_LIFETIME: Duration = Duration::from_secs(3600);

#[derive(Clone, Copy)]
pub struct Collector {
    address: SocketAddrV4,
    pub pin: [u8; 32],
}

impl Collector {
    pub fn parse(address: &str, pin: &str) -> io::Result<Self> {
        let address: SocketAddrV4 = address
            .parse()
            .ok()
            .filter(|address: &SocketAddrV4| {
                address.port() != 0
                    && !address.ip().is_unspecified()
                    && !address.ip().is_multicast()
            })
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "expected collector IPv4:port")
            })?;
        let mut digest = [0; 32];
        if pin.len() != 64 || !pin.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "expected 64 hex digits for collector pin",
            ));
        }
        for (index, byte) in digest.iter_mut().enumerate() {
            *byte = u8::from_str_radix(&pin[2 * index..2 * index + 2], 16)
                .map_err(|_| io::ErrorKind::InvalidInput)?;
        }
        if digest == [0; 32] {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "empty collector pin",
            ));
        }
        Ok(Self {
            address,
            pin: digest,
        })
    }
}

pub struct Config {
    pub apn: String,
    pub collector: Option<Collector>,
}

#[derive(Default)]
struct DialState {
    generation: Option<u32>,
    requested: bool,
    ready: Option<io::Result<TcpStream>>,
    stopping: bool,
}

/// A blocking TCP dial must not hold up the communications owner or BLE.
pub struct Dialer {
    state: Arc<Mutex<DialState>>,
    task: Option<JoinHandle<()>>,
}

impl Dialer {
    pub fn start(collector: Collector) -> io::Result<Self> {
        let state = Arc::new(Mutex::new(DialState::default()));
        let worker = state.clone();
        let notify = thread::current();
        let task = thread::Builder::new()
            .name("tracker-collector".into())
            .stack_size(16 * 1024)
            .spawn(move || loop {
                let request = {
                    let mut state = worker.lock().unwrap();
                    if state.stopping {
                        break;
                    }
                    if std::mem::take(&mut state.requested) {
                        state.generation
                    } else {
                        None
                    }
                };
                if let Some(generation) = request {
                    let result = TcpStream::connect_timeout(
                        &SocketAddr::V4(collector.address),
                        CONNECT_TIMEOUT,
                    );
                    let mut state = worker.lock().unwrap();
                    if state.generation == Some(generation) && !state.stopping {
                        state.ready = Some(result);
                        drop(state);
                        notify.unpark();
                    }
                } else {
                    thread::park();
                }
            })?;
        Ok(Self {
            state,
            task: Some(task),
        })
    }

    pub fn set_link(&self, generation: Option<u32>) {
        let stale = {
            let mut state = self.state.lock().unwrap();
            state.generation = generation;
            state.requested = false;
            state.ready.take()
        };
        drop(stale);
    }

    pub fn request(&self) {
        self.state.lock().unwrap().requested = true;
        self.task.as_ref().unwrap().thread().unpark();
    }

    pub fn receive(&self) -> Option<io::Result<TcpStream>> {
        self.state.lock().unwrap().ready.take()
    }

    pub fn stop(&mut self) -> io::Result<()> {
        if let Some(task) = self.task.take() {
            self.state.lock().unwrap().stopping = true;
            task.thread().unpark();
            task.join()
                .map_err(|_| io::Error::other("tracker collector dial task panicked"))?;
        }
        Ok(())
    }
}

impl Drop for Dialer {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

pub struct Retry {
    pub next: Instant,
    delay: Duration,
}

impl Retry {
    pub fn new() -> Self {
        Self {
            next: Instant::now(),
            delay: Duration::from_secs(1),
        }
    }

    pub fn later(&mut self) {
        self.next = Instant::now() + self.delay;
        self.delay = (self.delay * 2).min(Duration::from_secs(30));
    }
}

pub struct Upload<'a> {
    connection: tls::Connection<'a>,
    ready: bool,
}

impl<'a> Upload<'a> {
    pub fn open(
        identity: &'a tls::Identity,
        socket: TcpStream,
        collector: Collector,
    ) -> io::Result<Self> {
        let connection = identity
            .connect_pinned(socket, collector.pin, SESSION_LIFETIME)
            .map_err(|error| io::Error::other(format!("collector TLS: {error:?}")))?;
        Ok(Self {
            connection,
            ready: false,
        })
    }

    pub fn service(
        &mut self,
        publication: &mut Publisher<Snapshot, Frame>,
        now: Instant,
    ) -> io::Result<bool> {
        if !self.ready {
            if !self
                .connection
                .poll()
                .map_err(|error| io::Error::other(format!("collector TLS: {error:?}")))?
            {
                return Ok(false);
            }
            self.ready = true;
            eprintln!("motion-tracker collector TLS online");
        }
        if !publication.in_flight() {
            // Detect a closed peer even with no sample waiting. Finish any
            // pending write before invoking another TLS operation.
            match self.connection.read(&mut [0]) {
                Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
                Ok(_) => return Err(io::ErrorKind::InvalidData.into()),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(error),
            }
        }
        publication.service(
            &mut self.connection,
            now,
            now + CONNECT_TIMEOUT,
            wire::frame,
        )
    }
}
