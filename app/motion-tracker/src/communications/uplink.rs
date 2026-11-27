//! Motion Tracker cellular link, reconnect policy and collector TCP dial worker.
use super::{Endpoint, Names};
use cellular::Status;
use std::io;
use std::net::{SocketAddr, TcpStream};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex, OnceLock,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);

pub struct Config {
    pub apn: String,
    pub collector: Option<Collector>,
}

#[derive(Clone, Copy)]
pub struct Collector {
    pub endpoint: Endpoint,
    pub interval: Duration,
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
    pub fn start(config: Config, names: Names) -> io::Result<Self> {
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
                        .name(names.collector_worker.into())
                        .stack_size(4 * 1024)
                        .spawn(move || dial(collector.endpoint, &control, names.product))
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
                .map_err(|_| io::Error::other("telemetry collector dial task panicked"))
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

fn dial(collector: Endpoint, signals: &Signals, product: &str) {
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
                log::warn!("{product} collector TCP: {}", error.kind());
                retry.later();
            }
        }
    }
}
