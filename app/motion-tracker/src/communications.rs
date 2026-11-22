//! One task steps both TLS sessions under the SDK's single identity lease.
use crate::{radio, screen::LinkStatus, tracker::Snapshot, uplink};
use std::cell::OnceCell;
use std::io;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const ACTIVE_POLL: Duration = Duration::from_millis(15);

#[derive(Clone, Copy, PartialEq, Eq)]
pub struct View {
    pub link: LinkStatus,
    pub key: Option<[u8; 32]>,
}

impl Default for View {
    fn default() -> Self {
        Self {
            link: LinkStatus::Idle,
            key: None,
        }
    }
}

pub enum Pairing {
    Start,
    Cancel,
}

#[derive(Default)]
struct Shared {
    stopping: bool,
    pairing: Option<Pairing>,
    latest: Option<Snapshot>,
    view: View,
}

pub struct Communications {
    shared: Arc<Mutex<Shared>>,
    task: Option<JoinHandle<io::Result<()>>>,
}

impl Communications {
    pub fn start(pair: bool, cellular: Option<uplink::Config>) -> io::Result<Self> {
        let shared = Arc::new(Mutex::new(Shared::default()));
        let incoming = shared.clone();
        let task = thread::Builder::new()
            .name("tracker-links".into())
            .stack_size(24 * 1024)
            .spawn(move || run(&incoming, pair, cellular))?;
        Ok(Self {
            shared,
            task: Some(task),
        })
    }

    pub fn pair(&self, command: Pairing) {
        self.shared.lock().unwrap().pairing = Some(command);
        self.task.as_ref().unwrap().thread().unpark();
    }

    pub fn offer(&self, sample: Snapshot) {
        if let Ok(mut shared) = self.shared.try_lock() {
            shared.latest = Some(sample);
            self.task.as_ref().unwrap().thread().unpark();
        }
    }

    pub fn view(&self, previous: View) -> View {
        self.shared
            .try_lock()
            .map_or(previous, |shared| shared.view)
    }

    pub fn stop(&mut self) -> io::Result<()> {
        if let Some(task) = self.task.take() {
            self.shared.lock().unwrap().stopping = true;
            task.thread().unpark();
            return task
                .join()
                .map_err(|_| io::Error::other("tracker links task panicked"))?;
        }
        Ok(())
    }
}

impl Drop for Communications {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn tls_identity(slot: &OnceCell<tls::Identity>) -> io::Result<&tls::Identity> {
    if slot.get().is_none() {
        let identity = tls::open().map_err(|error| io::Error::other(format!("TLS: {error:?}")))?;
        let _ = slot.set(identity);
    }
    Ok(slot.get().unwrap())
}

fn run(shared: &Mutex<Shared>, pair: bool, cellular: Option<uplink::Config>) -> io::Result<()> {
    let collector = cellular.as_ref().and_then(|config| config.collector);
    let mut modem = cellular.map(uplink::Modem::start).transpose()?;
    let identity = OnceCell::new();
    let mut radio = None;
    let mut next_radio = Instant::now();
    let mut upload = None;
    let mut retry = uplink::Retry::new();
    let mut retry_pending = false;
    let mut link_generation = 0;
    let mut pairing_failed = false;
    let mut latest = None;
    let mut command = pair.then_some(Pairing::Start);

    loop {
        let (sample, pair) = {
            let mut inbox = shared.lock().unwrap();
            if inbox.stopping {
                break;
            }
            (inbox.latest.take(), inbox.pairing.take().or(command.take()))
        };
        let now = Instant::now();
        if let Some(pair) = pair {
            radio = None;
            pairing_failed = false;
            if matches!(pair, Pairing::Start) {
                match tls_identity(&identity).and_then(radio::pair) {
                    Ok(output) => radio = Some(output),
                    Err(error) => {
                        eprintln!("motion-tracker pairing: {error}");
                        pairing_failed = true;
                    }
                }
            }
        }
        if radio.as_ref().is_some_and(|output| output.expired(now)) {
            radio = None;
        }
        let view = match radio.as_mut() {
            Some(output) => {
                if now >= next_radio {
                    output.service();
                    next_radio = now + ACTIVE_POLL;
                }
                if let Some(snapshot) = &sample {
                    output.offer(snapshot, now);
                }
                if output.live() {
                    View {
                        link: LinkStatus::Connected,
                        key: None,
                    }
                } else if output.paired() {
                    View {
                        link: LinkStatus::Paired,
                        key: None,
                    }
                } else {
                    View {
                        link: LinkStatus::Pairing,
                        key: Some(*output.key()),
                    }
                }
            }
            None if pairing_failed => View {
                link: LinkStatus::Unavailable,
                key: None,
            },
            None => View::default(),
        };
        shared.lock().unwrap().view = view;
        if let (Some(collector), Some(modem)) = (collector, modem.as_ref()) {
            if sample.is_some() {
                latest = sample;
            }
            let (generation, socket) = modem.receive();
            if generation != link_generation {
                link_generation = generation;
                upload = None;
                retry = uplink::Retry::new();
                retry_pending = false;
            }
            let progress = if let Some(socket) = socket {
                tls_identity(&identity)
                    .and_then(|identity| uplink::Upload::open(identity, socket, collector))
                    .map(|connection| {
                        upload = Some(connection);
                        false
                    })
            } else {
                upload
                    .as_mut()
                    .map_or(Ok(false), |connection| connection.service(&mut latest))
            };
            match progress {
                Ok(true) => retry = uplink::Retry::new(),
                Ok(false) => {}
                Err(error) => {
                    eprintln!("motion-tracker collector retry: {error}");
                    upload = None;
                    retry.later();
                    retry_pending = true;
                }
            }
            if retry_pending && now >= retry.next {
                modem.request_socket(generation);
                retry_pending = false;
            }
        }
        if radio.is_some() || upload.is_some() {
            thread::park_timeout(ACTIVE_POLL);
        } else if collector.is_some() {
            thread::park_timeout(Duration::from_millis(100));
        } else {
            thread::park();
        }
    }
    drop(upload);
    drop(radio);
    modem.as_mut().map_or(Ok(()), uplink::Modem::stop)
}
