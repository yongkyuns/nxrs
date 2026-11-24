//! Reconnect policy for live tracker telemetry. The modem task owns blocking I/O.
use super::{CellularConfig, Endpoint, publication::Publisher};
use crate::{
    text::Text,
    tracker::Snapshot,
    wire::cellular::{self as wire, MAX_BYTES, Summary},
};
use cellular::Session as _;
use std::io::{self, Read};
use std::net::{SocketAddr, TcpStream};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const LINK_POLL: Duration = Duration::from_millis(100);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const SESSION_LIFETIME: Duration = Duration::from_secs(3600);

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
    pub fn start(config: CellularConfig) -> io::Result<Self> {
        let signals = Arc::new(Signals {
            link_worker: thread::current(),
            stopping: AtomicBool::new(false),
            exchange: Mutex::new(Exchange {
                generation: 0,
                requested: false,
                socket: None,
            }),
        });
        let control = signals.clone();
        let task = thread::Builder::new()
            .name("tracker-modem".into())
            .stack_size(4 * 1024)
            .spawn(move || supervise(config, &control))?;
        Ok(Self {
            signals,
            task: Some(task),
        })
    }

    fn receive(&self) -> (u32, Option<TcpStream>) {
        let mut exchange = self.signals.exchange.lock().unwrap();
        (exchange.generation, exchange.socket.take())
    }

    fn request_socket(&self, generation: u32) {
        {
            let mut exchange = self.signals.exchange.lock().unwrap();
            if exchange.generation != generation || exchange.requested || exchange.socket.is_some()
            {
                return;
            }
            exchange.requested = true;
        }
        self.task.as_ref().unwrap().thread().unpark();
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

struct Retry {
    next: Instant,
    delay: Duration,
}

impl Retry {
    pub fn new() -> Self {
        Self {
            next: Instant::now(),
            delay: Duration::from_secs(1),
        }
    }

    fn later(&mut self) {
        self.next = Instant::now() + self.delay;
        self.delay = (self.delay * 2).min(Duration::from_secs(30));
    }
}

fn supervise(config: CellularConfig, signals: &Signals) -> io::Result<()> {
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
                        log::info!("motion-tracker cellular {status:?}");
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
                                            {
                                                let mut exchange = signals.exchange.lock().unwrap();
                                                exchange.requested = false;
                                                exchange.socket = Some(socket);
                                            }
                                            signals.link_worker.unpark();
                                            socket_retry = Retry::new();
                                        }
                                        Ok(_) => {}
                                        Err(error) => {
                                            log::warn!(
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
                {
                    // Close a pending socket outside the lock before stopping PPP.
                    let _retired_socket = {
                        let mut exchange = signals.exchange.lock().unwrap();
                        exchange.generation = exchange.generation.wrapping_add(1);
                        exchange.requested = false;
                        exchange.socket.take()
                    };
                }
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
        log::warn!("motion-tracker cellular retry: {failure}");
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

struct Upload<'a> {
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

    pub fn service(&mut self, publication: &mut Reports, now: Instant) -> io::Result<bool> {
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
        publication.service(&mut self.connection, now)
    }
}

struct Reports {
    publisher: Publisher<Summary, Text<MAX_BYTES>>,
    interval: Duration,
    next: Instant,
}

impl Reports {
    fn new(interval: Duration, now: Instant) -> Self {
        Self {
            publisher: Publisher::default(),
            interval,
            next: now,
        }
    }

    fn eligible(&self, now: Instant) -> bool {
        now >= self.next && !self.publisher.is_idle()
    }

    fn in_flight(&self) -> bool {
        self.publisher.in_flight()
    }

    fn service(&mut self, output: &mut impl std::io::Write, now: Instant) -> io::Result<bool> {
        if !self.in_flight() && !self.eligible(now) {
            return Ok(false);
        }
        self.publisher
            .service(output, now, now + CONNECT_TIMEOUT, |report| {
                let frame = wire::frame(report)?;
                // Charge each attempted frame before its first write. Stream
                // replacement can discard partial bytes but never refunds them.
                self.next = now + self.interval;
                Ok(frame)
            })
    }
}

pub struct Collector<'a, 'm> {
    endpoint: Endpoint,
    modem: &'m Modem,
    upload: Option<Upload<'a>>,
    reports: Reports,
    generation: u32,
    retry: Retry,
}

impl<'a, 'm> Collector<'a, 'm> {
    pub fn new(endpoint: Endpoint, interval: Duration, modem: &'m Modem) -> Self {
        Self {
            endpoint,
            modem,
            upload: None,
            reports: Reports::new(interval, Instant::now()),
            generation: 0,
            retry: Retry::new(),
        }
    }

    pub fn service(
        &mut self,
        sample: Option<Snapshot>,
        now: Instant,
        identity: impl FnOnce() -> io::Result<&'a tls::Identity>,
    ) {
        if let Some(sample) = sample {
            self.reports.publisher.offer(sample.into());
        }
        let (generation, socket) = self.modem.receive();
        if generation != self.generation {
            self.generation = generation;
            self.upload = None;
            self.reports.publisher.reset_stream();
            self.retry = Retry::new();
        }
        let result = if let Some(socket) = socket {
            identity()
                .and_then(|identity| Upload::open(identity, socket, self.endpoint))
                .map(|upload| {
                    self.upload = Some(upload);
                    false
                })
        } else if let Some(upload) = self.upload.as_mut() {
            upload.service(&mut self.reports, now)
        } else {
            Ok(false)
        };
        match result {
            Ok(true) => self.retry = Retry::new(),
            Ok(false) => {}
            Err(error) => {
                log::warn!("motion-tracker collector retry: {error}");
                self.upload = None;
                self.reports.publisher.reset_stream();
                self.retry.later();
            }
        }
        if self.upload.is_none() && self.reports.eligible(now) && now >= self.retry.next {
            self.modem.request_socket(generation);
        }
    }

    pub fn wake_in(&self, now: Instant) -> Option<Duration> {
        if self.upload.is_some() {
            return Some(super::ACTIVE_POLL);
        }
        let due = self.reports.next.max(self.retry.next);
        (now < due && !self.reports.publisher.is_idle()).then(|| due.saturating_duration_since(now))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tracker::Reading;

    #[test]
    fn failed_send_spends_the_slot_and_reconnect_sends_only_the_latest_summary() {
        let now = Instant::now();
        let interval = Duration::from_secs(60);
        let mut reports = Reports::new(interval, now);
        let summary = |sequence| {
            Summary::from(Snapshot {
                imu: [Reading::Stale, Reading::Fault],
                gnss: Reading::Live(gnss::GnssFix {
                    sequence,
                    timestamp_ms: u64::MAX,
                    receiver_utc_ms: None,
                    latitude_deg: -90.0,
                    longitude_deg: -180.0,
                    speed_mps: f32::MAX,
                    course_rad: None,
                }),
                clock: None,
            })
        };

        struct Broken;
        impl std::io::Write for Broken {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> {
                Err(io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        reports.publisher.offer(summary(1));
        assert!(reports.service(&mut Broken, now).is_err());
        reports.publisher.reset_stream();
        reports.publisher.offer(summary(2));
        reports.publisher.offer(summary(u64::MAX));

        let mut bytes = Vec::new();
        assert!(
            !reports
                .service(&mut bytes, now + interval - Duration::from_millis(1))
                .unwrap()
        );
        assert!(bytes.is_empty());
        assert!(reports.service(&mut bytes, now + interval).unwrap());
        assert!(!reports.service(&mut bytes, now + interval).unwrap());
        assert!(bytes.len() <= MAX_BYTES);
        let frame = std::str::from_utf8(&bytes).unwrap();
        assert!(frame.starts_with("{\"v\":1,\"kind\":\"summary\",\"imu\":[\"stale\",\"fault\"],"));
        assert!(frame.contains(&format!("\"seq\":{}", u64::MAX)));
        assert_eq!(frame.lines().count(), 1);
    }
}
