//! Reconnect policy for live tracker telemetry. The modem task owns blocking I/O.
use crate::{text::Text, tracker::Snapshot, wire};
use cellular::Session as _;
use std::io::{self, Read};
use std::net::{SocketAddr, SocketAddrV4, TcpStream};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use telemetry_service::stream::Publisher;

const LINK_POLL: Duration = Duration::from_millis(100);
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

struct Signals {
    link_worker: thread::Thread,
    stopping: AtomicBool,
    exchange: Mutex<Exchange>,
}

struct Exchange {
    generation: u32,
    requested: bool,
    socket: Option<TcpStream>,
}

pub struct Modem {
    signals: Arc<Signals>,
    task: Option<JoinHandle<io::Result<()>>>,
}

impl Modem {
    pub fn start(config: Config) -> io::Result<Self> {
        let signals = Arc::new(Signals {
            link_worker: thread::current(),
            stopping: AtomicBool::new(false),
            exchange: Mutex::new(Exchange {
                generation: 0,
                requested: config.collector.is_some(),
                socket: None,
            }),
        });
        let control = signals.clone();
        let task = thread::Builder::new()
            .name("tracker-modem".into())
            .stack_size(16 * 1024)
            .spawn(move || supervise(config, &control))?;
        Ok(Self {
            signals,
            task: Some(task),
        })
    }

    pub fn receive(&self) -> (u32, Option<TcpStream>) {
        let mut exchange = self.signals.exchange.lock().unwrap();
        (exchange.generation, exchange.socket.take())
    }

    pub fn request_socket(&self, generation: u32) {
        let mut exchange = self.signals.exchange.lock().unwrap();
        if exchange.generation == generation && exchange.socket.is_none() {
            exchange.requested = true;
            drop(exchange);
            self.task.as_ref().unwrap().thread().unpark();
        }
    }

    pub fn stop(&mut self) -> io::Result<()> {
        self.signals.stopping.store(true, Ordering::Release);
        if let Some(task) = self.task.take() {
            task.thread().unpark();
            task.join()
                .map_err(|_| io::Error::other("tracker modem task panicked"))??;
        }
        Ok(())
    }
}

impl Drop for Modem {
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

fn supervise(config: Config, signals: &Signals) -> io::Result<()> {
    use cellular::{Error, Status};
    let mut retry = Retry::new();
    while !signals.stopping.load(Ordering::Acquire) {
        if Instant::now() < retry.next {
            thread::park_timeout(retry.next.saturating_duration_since(Instant::now()));
            continue;
        }

        let failure = match cellular::connect(&config.apn) {
            Ok(mut session) => {
                let mut reported = None;
                let mut socket_retry = Retry::new();
                while !signals.stopping.load(Ordering::Acquire) {
                    let status = session.status();
                    if reported != Some(status) {
                        if matches!(status, Status::Online { .. }) {
                            retry = Retry::new();
                        }
                        eprintln!("motion-tracker cellular {status:?}");
                        reported = Some(status);
                    }
                    match status {
                        Status::Online { .. } => {
                            if let Some(collector) = config.collector {
                                if signals.exchange.lock().unwrap().requested
                                    && Instant::now() >= socket_retry.next
                                {
                                    match TcpStream::connect_timeout(
                                        &SocketAddr::V4(collector.address),
                                        CONNECT_TIMEOUT,
                                    ) {
                                        Ok(socket)
                                            if matches!(
                                                session.status(),
                                                Status::Online { .. }
                                            ) =>
                                        {
                                            let mut exchange = signals.exchange.lock().unwrap();
                                            exchange.requested = false;
                                            exchange.socket = Some(socket);
                                            drop(exchange);
                                            signals.link_worker.unpark();
                                            socket_retry = Retry::new();
                                        }
                                        Ok(_) => {}
                                        Err(error) => {
                                            eprintln!(
                                                "motion-tracker collector TCP: {}",
                                                error.kind()
                                            );
                                            socket_retry.later();
                                        }
                                    }
                                }
                            }
                        }
                        Status::Connecting => {}
                        Status::Failed(_) | Status::Stopped => break,
                    }
                    thread::park_timeout(LINK_POLL);
                }
                let socket = {
                    let mut exchange = signals.exchange.lock().unwrap();
                    exchange.generation = exchange.generation.wrapping_add(1);
                    exchange.requested = config.collector.is_some();
                    exchange.socket.take()
                };
                drop(socket);
                signals.link_worker.unpark();
                let result = session.stop();
                if signals.stopping.load(Ordering::Acquire) {
                    // Expected network failures have already been handed to the retry policy.
                    return match result {
                        Err(error) if fatal(error) => Err(io::Error::other(error)),
                        _ => Ok(()),
                    };
                }
                result.err().unwrap_or(Error::LinkLost)
            }
            Err(error) => error,
        };
        eprintln!("motion-tracker cellular retry: {failure}");
        if fatal(failure) {
            return Err(io::Error::other(failure));
        }
        retry.later();
    }
    Ok(())
}

fn fatal(error: cellular::Error) -> bool {
    matches!(
        error,
        cellular::Error::InvalidApn
            | cellular::Error::Unsupported
            | cellular::Error::RecoveryRequired
    )
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
        publication: &mut Publisher<Snapshot, Text<768>>,
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
