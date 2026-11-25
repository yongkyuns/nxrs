//! Reconnect policy for live tracker telemetry. The collector dial task owns TCP connect waits.
use super::{publication::Publisher, wire};
use crate::{text::Text, tracker::Snapshot};
use cellular::Status;
use std::io::{self, Read};
use std::net::{SocketAddr, SocketAddrV4, TcpStream};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex, OnceLock,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const SESSION_LIFETIME: Duration = Duration::from_secs(3600);

#[derive(Clone, Copy)]
pub struct Endpoint {
    pub address: SocketAddrV4,
    pub pin: [u8; 32],
}

impl Endpoint {
    pub fn parse(address: &str, pin: &str) -> io::Result<Self> {
        let address: SocketAddrV4 = address
            .parse()
            .ok()
            .filter(|address: &SocketAddrV4| {
                address.port() != 0
                    && !address.ip().is_unspecified()
                    && !address.ip().is_multicast()
            })
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "expected IPv4:port"))?;
        let mut digest = [0; 32];
        if pin.len() != 64 || !pin.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "expected 64 hex digits for the TLS pin",
            ));
        }
        for (index, byte) in digest.iter_mut().enumerate() {
            *byte = u8::from_str_radix(&pin[2 * index..2 * index + 2], 16)
                .map_err(|_| io::ErrorKind::InvalidInput)?;
        }
        if digest == [0; 32] {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "empty TLS pin"));
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

#[derive(Clone, Copy)]
pub struct Collector {
    pub endpoint: Endpoint,
    pub interval: Duration,
}

pub struct Reports {
    interval: Duration,
    next: Instant,
    publication: Publisher<Snapshot, Text<{ wire::CELLULAR_FRAME_BYTES }>>,
}

impl Reports {
    pub fn new(interval: Duration, now: Instant) -> Self {
        Self {
            interval,
            next: now,
            publication: Publisher::default(),
        }
    }

    pub fn reset_stream(&mut self) {
        self.publication.reset_stream();
    }

    fn service(
        &mut self,
        output: &mut impl io::Write,
        now: Instant,
        snapshot: impl FnOnce() -> Snapshot,
    ) -> io::Result<bool> {
        if !self.publication.in_flight() && now >= self.next {
            // A failed or partial attempt spends this interval too. Replacing
            // the TLS stream does not refresh the allowance.
            self.next = now + self.interval;
            self.publication.offer(snapshot());
        }
        self.publication
            .service(output, now, now + CONNECT_TIMEOUT, wire::cellular_frame)
    }
}

struct Signals {
    link_worker: thread::Thread,
    dial_worker: OnceLock<thread::Thread>,
    stopping: AtomicBool,
    exchange: Mutex<Exchange>,
}

struct Exchange {
    status: Status,
    generation: u32,
    requested: bool,
    socket: Option<TcpStream>,
}

impl Signals {
    fn link_changed(&self, status: Status) {
        let retired = {
            let mut exchange = self.exchange.lock().unwrap();
            let lost =
                matches!(exchange.status, Status::Online { .. }) && exchange.status != status;
            exchange.status = status;
            if lost {
                exchange.generation = exchange.generation.wrapping_add(1);
                exchange.requested = true;
                exchange.socket.take()
            } else {
                None
            }
        };
        self.link_worker.unpark();
        if let Some(worker) = self.dial_worker.get() {
            worker.unpark();
        }
        // Socket retirement happens outside the shared slot's lock.
        drop(retired);
    }
}

pub struct Modem {
    signals: Arc<Signals>,
    task: Option<JoinHandle<()>>,
    link: Box<dyn cellular::Session>,
}

impl Modem {
    pub fn start(config: Config) -> io::Result<Self> {
        if config.collector.is_some() && !tls::Identity::SUPPORTS_PINNED_CLIENT {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "collector TLS is unavailable",
            ));
        }
        let signals = Arc::new(Signals {
            link_worker: thread::current(),
            dial_worker: OnceLock::new(),
            stopping: AtomicBool::new(false),
            exchange: Mutex::new(Exchange {
                status: Status::Connecting,
                generation: 0,
                requested: config.collector.is_some(),
                socket: None,
            }),
        });
        let observer = signals.clone();
        let link =
            cellular::connect_managed(&config.apn, move |status| observer.link_changed(status))
                .map_err(io::Error::other)?;
        let task = if tls::Identity::SUPPORTS_PINNED_CLIENT {
            config
                .collector
                .map(|collector| {
                    let control = signals.clone();
                    thread::Builder::new()
                        .name("tracker-collector".into())
                        .stack_size(4 * 1024)
                        .spawn(move || dial(collector.endpoint, &control))
                })
                .transpose()?
        } else {
            None
        };
        Ok(Self {
            signals,
            task,
            link: Box::new(link),
        })
    }

    pub fn status(&self) -> Status {
        self.link.status()
    }

    pub fn receive(&self) -> (u32, Option<TcpStream>) {
        let mut exchange = self.signals.exchange.lock().unwrap();
        (exchange.generation, exchange.socket.take())
    }

    pub fn request_socket(&self, generation: u32) {
        {
            let mut exchange = self.signals.exchange.lock().unwrap();
            if exchange.generation != generation || exchange.socket.is_some() {
                return;
            }
            exchange.requested = true;
        }
        if let Some(task) = &self.task {
            task.thread().unpark();
        }
    }

    pub fn stop(&mut self) -> io::Result<()> {
        self.signals.stopping.store(true, Ordering::Release);
        let dial_result = self.task.take().map_or(Ok(()), |task| {
            task.thread().unpark();
            task.join()
                .map_err(|_| io::Error::other("tracker collector dial task panicked"))
        });
        let link_result = self.link.stop().map_err(io::Error::other);
        dial_result.and(link_result)
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

fn dial(collector: Endpoint, signals: &Signals) {
    let _ = signals.dial_worker.set(thread::current());
    let mut generation = 0;
    let mut retry = Retry::new();
    while !signals.stopping.load(Ordering::Acquire) {
        let (current, requested) = {
            let exchange = signals.exchange.lock().unwrap();
            (
                exchange.generation,
                exchange.requested && matches!(exchange.status, Status::Online { .. }),
            )
        };
        if generation != current {
            generation = current;
            retry = Retry::new();
        }
        if !requested {
            thread::park();
            continue;
        }
        let now = Instant::now();
        if now < retry.next {
            thread::park_timeout(retry.next - now);
            continue;
        }
        match TcpStream::connect_timeout(&SocketAddr::V4(collector.address), CONNECT_TIMEOUT) {
            Ok(socket) => {
                let mut exchange = signals.exchange.lock().unwrap();
                if exchange.generation == generation
                    && matches!(exchange.status, Status::Online { .. })
                {
                    exchange.requested = false;
                    exchange.socket = Some(socket);
                    signals.link_worker.unpark();
                }
                retry = Retry::new();
            }
            Err(error) => {
                log::warn!("motion-tracker collector TCP: {}", error.kind());
                retry.later();
            }
        }
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
        collector: Endpoint,
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
        reports: &mut Reports,
        snapshot: impl FnOnce(Instant) -> Snapshot,
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
            log::info!("motion-tracker collector TLS online");
        }
        if !reports.publication.in_flight() {
            // Detect a closed peer even with no sample waiting. Finish any
            // pending write before invoking another TLS operation.
            match self.connection.read(&mut [0]) {
                Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
                Ok(_) => return Err(io::ErrorKind::InvalidData.into()),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(error),
            }
        }
        let now = Instant::now();
        reports.service(&mut self.connection, now, || snapshot(now))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tracker::Reading;

    #[test]
    fn report_budget_survives_an_interrupted_stream_and_skips_missed_intervals() {
        let now = Instant::now();
        let interval = Duration::from_secs(60);
        let mut reports = Reports::new(interval, now);
        let sample = || Snapshot {
            imu: [Reading::Waiting; 2],
            gnss: Reading::Waiting,
            clock: None,
        };
        assert_eq!(
            reports
                .service(&mut &mut [][..], now, sample)
                .unwrap_err()
                .kind(),
            io::ErrorKind::WriteZero
        );
        reports.reset_stream();
        let mut output = Vec::new();
        assert!(!reports
            .service(
                &mut output,
                now + Duration::from_secs(59),
                || unreachable!()
            )
            .unwrap());
        let later = now + 3 * interval;
        assert!(reports.service(&mut output, later, sample).unwrap());
        assert!(!reports
            .service(&mut output, later, || unreachable!())
            .unwrap());
        assert_eq!(output.iter().filter(|&&byte| byte == b'\n').count(), 1);
        assert!(reports
            .service(&mut output, later + interval, sample)
            .unwrap());
    }
}
