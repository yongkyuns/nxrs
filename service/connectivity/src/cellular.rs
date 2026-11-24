//! Reconnecting pinned TLS publication over a cellular HAL session.
use crate::{Endpoint, Publisher};
use ::cellular::{Error, Session, Status};
use std::io::{self, Read};
use std::net::TcpStream;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const ACTIVE_POLL: Duration = Duration::from_millis(15);
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

struct Modem {
    signals: Arc<Signals>,
    task: Option<JoinHandle<io::Result<()>>>,
}

impl Modem {
    fn start<S: Session>(
        builder: thread::Builder,
        endpoint: Option<Endpoint>,
        attach: impl Fn() -> Result<S, Error> + Send + 'static,
    ) -> io::Result<Self> {
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
        let task = builder.spawn(move || supervise(endpoint, attach, &control))?;
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

    fn stop(&mut self) -> io::Result<()> {
        self.signals.stopping.store(true, Ordering::Release);
        if let Some(task) = self.task.take() {
            task.thread().unpark();
            task.join()
                .map_err(|_| io::Error::other("cellular dial worker panicked"))??;
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
    fn new() -> Self {
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

fn supervise<S: Session>(
    endpoint: Option<Endpoint>,
    attach: impl Fn() -> Result<S, Error>,
    signals: &Signals,
) -> io::Result<()> {
    let mut retry = Retry::new();
    while !signals.stopping.load(Ordering::Acquire) {
        if Instant::now() < retry.next {
            thread::park_timeout(retry.next.saturating_duration_since(Instant::now()));
            continue;
        }

        let failure = match attach() {
            Ok(mut session) => {
                let mut reported = None;
                let mut socket_retry = Retry::new();
                while !signals.stopping.load(Ordering::Acquire) {
                    let status = session.status();
                    if reported != Some(status) {
                        if matches!(status, Status::Online { .. }) {
                            retry = Retry::new();
                        }
                        log::info!("cellular {status:?}");
                        reported = Some(status);
                    }
                    match status {
                        Status::Online { .. } => {
                            if let Some(collector) = endpoint {
                                if signals.exchange.lock().unwrap().requested
                                    && Instant::now() >= socket_retry.next
                                {
                                    match collector.connect(CONNECT_TIMEOUT) {
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
                                            log::warn!("collector TCP: {}", error.kind());
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
        log::warn!("cellular retry: {failure}");
        if fatal(failure) {
            return Err(io::Error::other(failure));
        }
        retry.later();
    }
    Ok(())
}

fn fatal(error: Error) -> bool {
    matches!(
        error,
        Error::InvalidApn | Error::Unsupported | Error::RecoveryRequired
    )
}

struct Upload<'a> {
    connection: tls::Connection<'a>,
    ready: bool,
}

impl<'a> Upload<'a> {
    fn open(
        identity: &'a tls::Identity,
        socket: TcpStream,
        collector: Endpoint,
    ) -> io::Result<Self> {
        let connection = collector
            .secure(identity, socket, SESSION_LIFETIME)
            .map_err(|error| io::Error::other(format!("collector TLS: {error:?}")))?;
        Ok(Self {
            connection,
            ready: false,
        })
    }

    fn service<Sample, Frame: AsRef<[u8]>>(
        &mut self,
        publication: &mut Reports<Sample, Frame>,
        now: Instant,
        encode: impl FnOnce(&Sample) -> io::Result<Frame>,
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
            log::info!("collector TLS online");
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
        publication.service(&mut self.connection, now, encode)
    }
}

struct Reports<Sample, Frame> {
    publisher: Publisher<Sample, Frame>,
    budget: Budget,
    next: Instant,
}

impl<Sample, Frame: AsRef<[u8]>> Reports<Sample, Frame> {
    fn new(budget: Budget, now: Instant) -> Self {
        Self {
            publisher: Publisher::default(),
            budget,
            next: now,
        }
    }

    fn eligible(&self, now: Instant) -> bool {
        now >= self.next && !self.publisher.is_idle()
    }

    fn in_flight(&self) -> bool {
        self.publisher.in_flight()
    }

    fn service(
        &mut self,
        output: &mut impl std::io::Write,
        now: Instant,
        encode: impl FnOnce(&Sample) -> io::Result<Frame>,
    ) -> io::Result<bool> {
        if !self.in_flight() && !self.eligible(now) {
            return Ok(false);
        }
        self.publisher
            .service(output, now, now + CONNECT_TIMEOUT, |report| {
                let frame = encode(report)?;
                if frame.as_ref().len() > self.budget.frame_bytes {
                    return Err(io::ErrorKind::InvalidInput.into());
                }
                // Charge each attempted frame before its first write. Stream
                // replacement can discard partial bytes but never refunds them.
                self.next = now
                    .checked_add(self.budget.interval)
                    .ok_or(io::ErrorKind::InvalidInput)?;
                Ok(frame)
            })
    }
}

/// Application payload limits for one publication stream.
#[derive(Clone, Copy)]
pub struct Budget {
    interval: Duration,
    frame_bytes: usize,
}

impl Budget {
    pub fn new(interval: Duration, frame_bytes: usize) -> io::Result<Self> {
        if interval.is_zero() || frame_bytes == 0 || Instant::now().checked_add(interval).is_none()
        {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        Ok(Self {
            interval,
            frame_bytes,
        })
    }
}

/// A single collector destination and its application-payload budget.
pub struct Destination {
    pub endpoint: Endpoint,
    pub budget: Budget,
}

/// Owns one cellular link and its optional collector. Step it from the
/// caller's wait loop. The existing dial worker isolates blocking TCP setup.
pub struct Uplink<'a, Sample, Frame> {
    collector: Option<Collector<'a, Sample, Frame>>,
    modem: Modem,
}

impl<'a, Sample, Frame: AsRef<[u8]>> Uplink<'a, Sample, Frame> {
    pub fn start<S: Session>(
        builder: thread::Builder,
        destination: Option<Destination>,
        attach: impl Fn() -> Result<S, Error> + Send + 'static,
    ) -> io::Result<Self> {
        let modem = Modem::start(
            builder,
            destination.as_ref().map(|target| target.endpoint),
            attach,
        )?;
        Ok(Self {
            collector: destination.map(Collector::new),
            modem,
        })
    }

    pub fn service(
        &mut self,
        sample: Option<Sample>,
        now: Instant,
        identity: impl FnOnce() -> io::Result<&'a tls::Identity>,
        encode: impl FnOnce(&Sample) -> io::Result<Frame>,
    ) {
        if let Some(collector) = &mut self.collector {
            collector.service(&self.modem, sample, now, identity, encode);
        }
    }

    pub fn wake_in(&self, now: Instant) -> Option<Duration> {
        self.collector
            .as_ref()
            .and_then(|collector| collector.wake_in(now))
    }

    pub fn stop(mut self) -> io::Result<()> {
        self.collector = None;
        self.modem.stop()
    }
}

struct Collector<'a, Sample, Frame> {
    endpoint: Endpoint,
    upload: Option<Upload<'a>>,
    reports: Reports<Sample, Frame>,
    generation: u32,
    retry: Retry,
}

impl<'a, Sample, Frame: AsRef<[u8]>> Collector<'a, Sample, Frame> {
    fn new(destination: Destination) -> Self {
        Self {
            endpoint: destination.endpoint,
            upload: None,
            reports: Reports::new(destination.budget, Instant::now()),
            generation: 0,
            retry: Retry::new(),
        }
    }

    fn service(
        &mut self,
        modem: &Modem,
        sample: Option<Sample>,
        now: Instant,
        identity: impl FnOnce() -> io::Result<&'a tls::Identity>,
        encode: impl FnOnce(&Sample) -> io::Result<Frame>,
    ) {
        if let Some(sample) = sample {
            self.reports.publisher.offer(sample);
        }
        let (generation, socket) = modem.receive();
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
            upload.service(&mut self.reports, now, encode)
        } else {
            Ok(false)
        };
        match result {
            Ok(true) => self.retry = Retry::new(),
            Ok(false) => {}
            Err(error) => {
                log::warn!("collector retry: {error}");
                self.upload = None;
                self.reports.publisher.reset_stream();
                self.retry.later();
            }
        }
        if self.upload.is_none() && self.reports.eligible(now) && now >= self.retry.next {
            modem.request_socket(generation);
        }
    }

    fn wake_in(&self, now: Instant) -> Option<Duration> {
        if self.upload.is_some() {
            return Some(ACTIVE_POLL);
        }
        let due = self.reports.next.max(self.retry.next);
        (now < due && !self.reports.publisher.is_idle()).then(|| due.saturating_duration_since(now))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    struct Broken;
    impl Write for Broken {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::ErrorKind::BrokenPipe.into())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn reconnect_keeps_the_rate_limit_and_only_the_latest_sample() {
        let now = Instant::now();
        let interval = Duration::from_secs(60);
        let mut reports = Reports::new(Budget::new(interval, 4).unwrap(), now);
        let encode = |sample: &&'static [u8]| Ok(*sample);
        reports.publisher.offer(&b"old"[..]);
        assert!(reports.service(&mut Broken, now, encode).is_err());
        reports.publisher.reset_stream();
        reports.publisher.offer(&b"new"[..]);
        reports.publisher.offer(&b"last"[..]);

        let mut bytes = Vec::new();
        assert!(
            !reports
                .service(
                    &mut bytes,
                    now + interval - Duration::from_millis(1),
                    encode
                )
                .unwrap()
        );
        assert!(bytes.is_empty());
        assert!(reports.service(&mut bytes, now + interval, encode).unwrap());
        assert_eq!(bytes, b"last");
        reports.publisher.offer(&b"oversized"[..]);
        assert_eq!(
            reports
                .service(&mut bytes, now + 2 * interval, encode)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(bytes, b"last");
    }
}
