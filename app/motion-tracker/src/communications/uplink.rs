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

#[derive(Clone)]
pub struct Config {
    pub apn: String,
    pub collector: Option<Collector>,
}

#[derive(Clone, Copy)]
pub struct Collector {
    pub endpoint: Endpoint,
    pub interval: Duration,
}

/// Resource admission is distinct from the HAL's managed network recovery.
/// Busy/I/O gets two bounded retries within this owner lifetime; terminal
/// configuration/quarantine failures remain visible while BLE keeps running.
pub struct Cellular {
    config: Option<Config>,
    modem: Option<Modem>,
    status: Option<Status>,
    next: Option<Instant>,
    attempts: u8,
}

impl Cellular {
    pub fn new(config: Option<Config>) -> Self {
        let requested = config.is_some();
        Self {
            config,
            modem: None,
            status: requested.then_some(Status::Connecting),
            next: requested.then(Instant::now),
            attempts: 0,
        }
    }

    pub fn admit(&mut self, now: Instant) {
        self.admit_with(now, Modem::start);
    }

    fn admit_with(
        &mut self,
        now: Instant,
        start: impl FnOnce(Config) -> Result<Modem, cellular::Error>,
    ) {
        if self.next.is_none_or(|next| now < next) {
            return;
        }
        self.next = None;
        self.attempts += 1;
        match start(self.config.as_ref().unwrap().clone()) {
            Ok(modem) => {
                self.modem = Some(modem);
                self.config = None;
            }
            Err(error) => {
                log::warn!(
                    "motion-tracker cellular admission {}: {error}",
                    self.attempts
                );
                self.status = Some(
                    if matches!(error, cellular::Error::Busy | cellular::Error::Io)
                        && self.attempts < 3
                    {
                        self.next = Some(now + Duration::from_secs(u64::from(self.attempts)));
                        Status::Retrying(error)
                    } else {
                        self.config = None;
                        Status::Failed(error)
                    },
                );
            }
        }
    }

    pub fn status(&self) -> Option<Status> {
        self.modem.as_ref().map(Modem::status).or(self.status)
    }

    pub fn modem(&self) -> Option<&Modem> {
        self.modem.as_ref()
    }

    pub fn next_admission(&self) -> Option<Instant> {
        self.next
    }

    pub fn request_stop(&mut self) {
        self.next = None;
        if let Some(modem) = &mut self.modem {
            modem.request_stop();
        }
    }

    pub fn stop(&mut self) -> io::Result<()> {
        self.request_stop();
        self.modem.as_mut().map_or(Ok(()), Modem::stop)
    }
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
    pub fn start(config: Config) -> Result<Self, cellular::Error> {
        if config.collector.is_some() && !tls::Identity::SUPPORTS_PINNED_CLIENT {
            return Err(cellular::Error::Unsupported);
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
        // Admit the dial worker first. If spawning fails, no modem has begun
        // acquisition whose rollback could hold up the independent BLE owner.
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
                .transpose()
                .map_err(|_| cellular::Error::Io)?
        } else {
            None
        };
        let observer = signals.clone();
        let link = match cellular::connect_managed(&config.apn, move |status| {
            observer.link_changed(status)
        }) {
            Ok(link) => link,
            Err(error) => {
                signals.stopping.store(true, Ordering::Release);
                if let Some(task) = task {
                    task.thread().unpark();
                    let _ = task.join();
                }
                return Err(error);
            }
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

    pub fn request_stop(&mut self) {
        self.signals.stopping.store(true, Ordering::Release);
        if let Some(task) = &self.task {
            task.thread().unpark();
        }
        // UART/PPP cancellation must begin before waiting for an in-flight
        // TCP connect. Their independent budgets overlap during teardown.
        self.link.request_stop();
    }

    pub fn stop(&mut self) -> io::Result<()> {
        self.request_stop();
        let dial_result = self.task.take().map_or(Ok(()), |task| {
            task.join()
                .map_err(|_| io::Error::other("tracker collector dial task panicked"))
        });
        let link_result = self.link.join().map_err(io::Error::other);
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
    fn admission_retries_are_finite_and_cancellation_prevents_another_acquisition() {
        let mut cellular = Cellular::new(Some(Config {
            apn: "test".into(),
            collector: None,
        }));
        let now = Instant::now();
        cellular.admit_with(now, |_| Err(cellular::Error::Busy));
        assert_eq!(
            cellular.status(),
            Some(Status::Retrying(cellular::Error::Busy))
        );
        assert_eq!(
            cellular.next_admission(),
            Some(now + Duration::from_secs(1))
        );
        cellular.admit_with(now, |_| panic!("admission before retry budget"));
        cellular.admit_with(now + Duration::from_secs(1), |_| Err(cellular::Error::Io));
        assert_eq!(
            cellular.next_admission(),
            Some(now + Duration::from_secs(3))
        );
        cellular.admit_with(now + Duration::from_secs(3), |_| Err(cellular::Error::Busy));
        assert_eq!(
            cellular.status(),
            Some(Status::Failed(cellular::Error::Busy))
        );
        assert_eq!(cellular.next_admission(), None);
        cellular.admit_with(now + Duration::from_secs(60), |_| {
            panic!("unbounded admission retry")
        });

        let mut cancelled = Cellular::new(Some(Config {
            apn: "test".into(),
            collector: None,
        }));
        cancelled.request_stop();
        cancelled.admit_with(now + Duration::from_secs(60), |_| {
            panic!("acquisition after stop")
        });
        for error in [
            cellular::Error::Unsupported,
            cellular::Error::InvalidApn,
            cellular::Error::RecoveryRequired,
        ] {
            let mut refused = Cellular::new(Some(Config {
                apn: "test".into(),
                collector: None,
            }));
            refused.admit_with(Instant::now(), |_| Err(error));
            assert_eq!(refused.status(), Some(Status::Failed(error)));
            assert_eq!(refused.next_admission(), None);
        }
    }

    #[test]
    fn report_budget_survives_an_interrupted_stream_and_skips_missed_intervals() {
        let now = Instant::now();
        let interval = Duration::from_secs(60);
        let mut reports = Reports::new(interval, now);
        let sample = || Snapshot {
            imu: [Reading::Waiting; 2],
            gnss: Reading::Waiting,
            clock: None,
            output_faults: 0,
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

    #[test]
    fn modem_cancellation_precedes_the_collector_join() {
        struct Link(Arc<AtomicBool>);
        impl cellular::Session for Link {
            fn status(&self) -> Status {
                Status::Connecting
            }
            fn request_stop(&mut self) {
                self.0.store(true, Ordering::Release);
            }
            fn is_finished(&self) -> bool {
                self.0.load(Ordering::Acquire)
            }
            fn join(&mut self) -> Result<(), cellular::Error> {
                assert!(self.is_finished());
                Ok(())
            }
        }
        let cancelled = Arc::new(AtomicBool::new(false));
        let observed = cancelled.clone();
        let task = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(1);
            while !observed.load(Ordering::Acquire) && Instant::now() < deadline {
                thread::yield_now();
            }
            assert!(
                observed.load(Ordering::Acquire),
                "modem must be cancelled before joining the dial"
            );
        });
        let mut modem = Modem {
            signals: Arc::new(Signals {
                link_worker: thread::current(),
                dial_worker: OnceLock::new(),
                stopping: AtomicBool::new(false),
                exchange: Mutex::new(Exchange {
                    status: Status::Connecting,
                    generation: 0,
                    requested: false,
                    socket: None,
                }),
            }),
            task: Some(task),
            link: Box::new(Link(cancelled)),
        };
        modem.stop().unwrap();
    }
}
