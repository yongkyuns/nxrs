//! One TLS owner; capture uses only try-lock transfers of bounded owned data.
use crate::Error;
use camera::{Capture, Frame};
use std::io::{self, Read};
use std::net::{TcpListener, TcpStream};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    mpsc::sync_channel,
    Arc, Mutex, MutexGuard, TryLockError,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use stream_service::{HttpSession, Metadata, Options, Progress, ServerStats};

/// Product-owned, approved enrollment. Construct this on the TLS thread.
/// `time` must be bounded and supply a currently authenticated observation,
/// accounting for its original age; recreating a lease cannot renew stale UTC.
/// It is called at startup, on admission and once per second while connected.
pub struct TlsIdentity<T> {
    pub identity: ::tls::Identity,
    pub policy: ::tls::CertificatePolicy,
    pub own: Vec<u8>,
    pub issuer: Vec<u8>,
    pub approved_peer: Vec<u8>,
    pub revocations: Vec<u8>,
    pub time: T,
}
struct Context<T> {
    identity: ::tls::Identity,
    time: T,
    request_timeout: Duration,
}
#[derive(Clone, Copy, Debug)]
pub struct TlsOptions {
    pub stack_bytes: usize,
    pub poll_interval: Duration,
}
#[derive(Clone, Copy, Debug, Default)]
pub struct TlsStats {
    pub handshakes: u64,
    pub failures: u64,
    pub skipped_busy: u64,
    pub last_failure: Option<::tls::Error>,
}
struct Packet<B> {
    bytes: B,
    frame: Option<(u64, Capture)>,
    metadata: Box<[u8]>,
    description: Option<(u64, usize, &'static str)>,
    epoch: usize,
}
struct State<B> {
    update: Option<crate::update::Update>,
    pending: Option<Packet<B>>,
    returned: Option<Packet<B>>,
    clock: Option<(u64, Instant)>,
    stream: ServerStats,
    tls: TlsStats,
    progress: Progress,
    worker_now: u64,
}
struct Shared<B> {
    state: Mutex<State<B>>,
    // Even generations are offline. Transitions cannot be lost when an
    // offline/online cycle occurs during a synchronous native call.
    epoch: AtomicUsize,
    stop: AtomicBool,
    failed: AtomicBool,
}
pub struct Worker<B> {
    shared: Arc<Shared<B>>,
    thread: Option<JoinHandle<()>>,
    free: Option<Packet<B>>,
    last_now: Option<u64>,
    epoch: usize,
    stats: (ServerStats, TlsStats),
    skipped_busy: u64,
}
impl<B> Worker<B> {
    pub fn enable_updates(&mut self, update: crate::update::Update) -> Result<(), Error> {
        if self.last_now.is_some() || self.stopped() {
            return Err(Error::Worker);
        }
        let mut state = lock(&self.shared);
        if state.update.is_some() {
            return Err(Error::Worker);
        }
        state.update = Some(update);
        Ok(())
    }
    pub fn stats(&self) -> (ServerStats, TlsStats) {
        let mut stats = self.stats;
        stats.1.skipped_busy = self.skipped_busy;
        stats
    }
    pub fn stopped(&self) -> bool {
        self.shared.stop.load(Ordering::Acquire)
            || self.thread.as_ref().is_none_or(JoinHandle::is_finished)
    }
    pub fn stop(&mut self) -> Result<(), Error> {
        self.shared.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            thread.thread().unpark();
            if thread.join().is_err() {
                self.shared.failed.store(true, Ordering::Release);
            }
        }
        let state = lock(&self.shared);
        self.stats = (state.stream, state.tls);
        if self.shared.failed.load(Ordering::Acquire) {
            Err(Error::Worker)
        } else {
            Ok(())
        }
    }
    fn refuse(&self, error: ::tls::Error) -> Error {
        self.shared.stop.store(true, Ordering::Release);
        self.wake();
        Error::Tls(error)
    }
    fn fail(&self) -> Error {
        self.shared.failed.store(true, Ordering::Release);
        self.shared.stop.store(true, Ordering::Release);
        self.wake();
        Error::Worker
    }
    fn wake(&self) {
        if let Some(thread) = &self.thread {
            thread.thread().unpark();
        }
    }
}
impl<B> Drop for Worker<B> {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}
impl<B: AsRef<[u8]> + AsMut<[u8]>> Worker<B> {
    pub fn spawn<T: FnMut() -> Result<::tls::TimeLease, ::tls::Error> + 'static>(
        listener: TcpListener,
        mut outgoing: B,
        mut handoff: B,
        options: Options,
        resources: TlsOptions,
        initialize: impl FnOnce() -> Result<TlsIdentity<T>, Error> + Send + 'static,
    ) -> Result<Self, Error>
    where
        B: Send + 'static,
    {
        if outgoing.as_ref().is_empty()
            || outgoing.as_ref().len() != handoff.as_ref().len()
            || handoff.as_ref().len() != handoff.as_mut().len()
            || outgoing.as_ref().len() != outgoing.as_mut().len()
            || resources.stack_bytes < 16384
            || !(Duration::from_millis(1)..=Duration::from_millis(100))
                .contains(&resources.poll_interval)
        {
            return Err(Error::Tls(::tls::Error::InvalidInput));
        }
        // Validate HTTP resources before starting capture or a native owner.
        let http = HttpSession::<TcpStream, _>::new(outgoing, options).map_err(Error::Network)?;
        listener.set_nonblocking(true).map_err(Error::Network)?;
        let shared = Arc::new(Shared {
            state: Mutex::new(State {
                update: None,
                pending: None,
                returned: None,
                clock: None,
                stream: ServerStats::default(),
                tls: TlsStats::default(),
                progress: Progress::default(),
                worker_now: 0,
            }),
            epoch: AtomicUsize::new(0),
            stop: AtomicBool::new(false),
            failed: AtomicBool::new(false),
        });
        let worker = shared.clone();
        let (ready, started) = sync_channel(1);
        let thread = thread::Builder::new()
            .name("camera-tls".into())
            .stack_size(resources.stack_bytes)
            .spawn(move || {
                let context = initialize().and_then(|mut context| {
                    let time = (context.time)().map_err(Error::Tls)?;
                    context
                        .identity
                        .configure(
                            ::tls::Certificates {
                                policy: &context.policy,
                                own: &context.own,
                                issuer: &context.issuer,
                                approved_peer: &context.approved_peer,
                                revocations: &context.revocations,
                            },
                            &time,
                        )
                        .map_err(Error::Tls)?;
                    // The SDK owns its parsed, validated enrollment. Release
                    // the input DER buffers before capture starts.
                    Ok(Context {
                        identity: context.identity,
                        time: context.time,
                        request_timeout: Duration::from_millis(options.request_timeout_ms),
                    })
                });
                let context = match context {
                    Ok(context) => {
                        let _ = ready.send(Ok(()));
                        context
                    }
                    Err(error) => {
                        let _ = ready.send(Err(error));
                        return;
                    }
                };
                run(&worker, listener, http, resources, context);
            })
            .map_err(|_| Error::Worker)?;
        // An initializer unwind drops the sender, so startup cannot wait forever
        // for an acknowledgement from a thread which has already exited.
        match started.recv().unwrap_or(Err(Error::Worker)) {
            Ok(()) => Ok(Self {
                shared,
                thread: Some(thread),
                free: Some(Packet {
                    bytes: handoff,
                    frame: None,
                    metadata: vec![0; stream_service::MAX_METADATA_BYTES].into_boxed_slice(),
                    description: None,
                    epoch: 0,
                }),
                last_now: None,
                epoch: 0,
                stats: Default::default(),
                skipped_busy: 0,
            }),
            Err(error) => {
                let _ = thread.join();
                Err(error)
            }
        }
    }
    pub fn poll(
        &mut self,
        now: u64,
        online: bool,
        frame: Option<Frame<'_>>,
        metadata: Option<Metadata<'_>>,
    ) -> Result<Progress, Error> {
        if self.stopped() || self.thread.as_ref().is_none_or(JoinHandle::is_finished) {
            return Err(Error::Worker);
        }
        if self.last_now.is_some_and(|last| now < last) {
            return Err(self.refuse(::tls::Error::Time));
        }
        self.last_now = Some(now);
        let observed = Instant::now();
        if online != (self.epoch & 1 != 0) {
            self.epoch = match self.epoch.checked_add(1) {
                Some(v) => v,
                None => {
                    self.shared.failed.store(true, Ordering::Release);
                    self.shared.stop.store(true, Ordering::Release);
                    self.wake();
                    return Err(Error::Worker);
                }
            };
            self.shared.epoch.store(self.epoch, Ordering::Release);
            self.wake();
        }
        let mut progress = Progress::default();
        // No waiting or spinning behind a preempted TLS worker.
        match self.shared.state.try_lock() {
            Ok(mut state) => {
                state.clock = Some((now, observed));
                self.stats = (state.stream, state.tls);
                progress = std::mem::take(&mut state.progress);
                if self.free.is_none() {
                    self.free = state.returned.take();
                }
            }
            Err(TryLockError::WouldBlock) => {}
            Err(TryLockError::Poisoned(_)) => return Err(self.fail()),
        }
        if !online {
            return Ok(Progress::default());
        }
        let Some(mut packet) = self.free.take() else {
            self.skipped_busy = self.skipped_busy.saturating_add(1);
            return Ok(progress);
        };
        packet.frame = None;
        packet.description = None;
        packet.epoch = self.epoch;
        // Copy outside the mailbox lock; the worker owns only transferred data.
        if let Some(frame) = frame {
            if frame.bytes.len() != frame.capture.len
                || frame.bytes.len() > packet.bytes.as_mut().len()
            {
                self.free = Some(packet);
                return Err(self.refuse(::tls::Error::Capacity));
            }
            packet.bytes.as_mut()[..frame.bytes.len()].copy_from_slice(frame.bytes);
            packet.frame = Some((frame.sequence, frame.capture));
        }
        if let Some(metadata) = metadata {
            if metadata.bytes.len() > packet.metadata.len() {
                self.free = Some(packet);
                return Err(self.refuse(::tls::Error::Capacity));
            }
            packet.metadata[..metadata.bytes.len()].copy_from_slice(metadata.bytes);
            packet.description = Some((
                metadata.revision,
                metadata.bytes.len(),
                metadata.content_type,
            ));
        }
        match self.shared.state.try_lock() {
            Ok(mut state) => {
                state.clock = Some((now, observed));
                state.pending = Some(packet);
            }
            Err(TryLockError::WouldBlock) => {
                self.free = Some(packet);
            }
            Err(TryLockError::Poisoned(_)) => {
                self.free = Some(packet);
                return Err(self.fail());
            }
        }
        self.wake();
        Ok(progress)
    }
}

fn lock<B>(shared: &Shared<B>) -> MutexGuard<'_, State<B>> {
    shared.state.lock().unwrap_or_else(|e| {
        shared.failed.store(true, Ordering::Release);
        shared.stop.store(true, Ordering::Release);
        e.into_inner()
    })
}
fn current<B>(shared: &Shared<B>, epoch: usize) -> bool {
    !shared.stop.load(Ordering::Acquire) && shared.epoch.load(Ordering::Acquire) == epoch
}
fn now<B>(shared: &Shared<B>) -> u64 {
    let mut state = lock(shared);
    let next = state.clock.map_or(0, |(ms, at)| {
        ms.saturating_add(at.elapsed().as_millis().min(u64::MAX as u128) as u64)
    });
    state.worker_now = state.worker_now.max(next);
    state.worker_now
}
#[derive(Default)]
struct Counters {
    tls: TlsStats,
    busy: u64,
}
fn reject_busy(listener: &TcpListener, counters: &mut Counters) {
    if let Ok((socket, _)) = listener.accept() {
        drop(socket);
        counters.busy = counters.busy.saturating_add(1);
    }
}
fn publish<B, W: std::io::Read + std::io::Write>(
    state: &mut State<B>,
    http: &HttpSession<W, B>,
    counters: &Counters,
) where
    B: AsRef<[u8]> + AsMut<[u8]>,
{
    state.stream = http.stats();
    state.stream.rejected_busy = state.stream.rejected_busy.saturating_add(counters.busy);
    state.tls = counters.tls;
}
fn run<B: AsRef<[u8]> + AsMut<[u8]>, T: FnMut() -> Result<::tls::TimeLease, ::tls::Error>>(
    shared: &Shared<B>,
    listener: TcpListener,
    mut http: HttpSession<TcpStream, B>,
    resources: TlsOptions,
    mut context: Context<T>,
) {
    let mut counters = Counters::default();
    while !shared.stop.load(Ordering::Acquire) {
        let epoch = shared.epoch.load(Ordering::Acquire);
        let clock_ready = lock(shared).clock.is_some();
        if epoch & 1 != 0 && clock_ready {
            match listener.accept() {
                Ok((socket, _)) => {
                    let (next, result) = serve(
                        shared,
                        &listener,
                        socket,
                        http,
                        resources,
                        &mut context,
                        epoch,
                        &mut counters,
                    );
                    http = next;
                    if let Err(error) = result {
                        counters.tls.failures = counters.tls.failures.saturating_add(1);
                        counters.tls.last_failure = Some(error);
                    }
                }
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(_) => {
                    shared.failed.store(true, Ordering::Release);
                    shared.stop.store(true, Ordering::Release);
                }
            }
        }
        let mut state = lock(shared);
        // Offline, handshake and failed peers cannot retain the sole handoff.
        if let Some(packet) = state.pending.take() {
            state.returned = Some(packet);
        }
        state.progress.active = false;
        publish(&mut state, &http, &counters);
        drop(state);
        if !shared.stop.load(Ordering::Acquire) {
            thread::park_timeout(resources.poll_interval);
        }
    }
}
#[allow(clippy::too_many_arguments)]
fn serve<B: AsRef<[u8]> + AsMut<[u8]>, T: FnMut() -> Result<::tls::TimeLease, ::tls::Error>>(
    shared: &Shared<B>,
    listener: &TcpListener,
    socket: TcpStream,
    storage: HttpSession<TcpStream, B>,
    resources: TlsOptions,
    context: &mut Context<T>,
    epoch: usize,
    counters: &mut Counters,
) -> (HttpSession<TcpStream, B>, Result<(), ::tls::Error>) {
    let connection = (context.time)().and_then(|time| context.identity.accept(socket, time));
    let mut connection = match connection {
        Ok(v) => v,
        Err(e) => return (storage, Err(e)),
    };
    loop {
        if !current(shared, epoch) {
            return (storage, Ok(()));
        }
        reject_busy(listener, counters);
        match connection.poll() {
            Ok(true) => break,
            Ok(false) => thread::park_timeout(resources.poll_interval),
            Err(error) => return (storage, Err(error)),
        }
    }
    counters.tls.handshakes = counters.tls.handshakes.saturating_add(1);
    // Multiplex only after exact enrolled-peer authentication. Preserve GET's
    // inspected prefix for the existing HTTP parser; no extra TLS context.
    let update = lock(shared).update.clone();
    let request_now = now(shared);
    let deadline = Instant::now() + context.request_timeout;
    let mut prefix = [0; 4];
    let mut used = 0;
    while update.is_some() && used < prefix.len() {
        if !current(shared, epoch) {
            return (storage, Ok(()));
        }
        if Instant::now() >= deadline {
            return (storage, Err(::tls::Error::Io));
        }
        match connection.read(&mut prefix[used..]) {
            Ok(0) => return (storage, Ok(())),
            Ok(n) => used += n,
            Err(e)
                if matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                thread::park_timeout(resources.poll_interval)
            }
            Err(_) => return (storage, Err(::tls::Error::Io)),
        }
    }
    if used == prefix.len() && &prefix == firmware_update::REQUEST {
        let Some(update) = update.filter(|v| !v.ready()) else {
            return (storage, Err(::tls::Error::InvalidInput));
        };
        let deadline = Instant::now() + update.timeout();
        let mut refreshed = Instant::now();
        let image = firmware_update::receive(&mut connection, update.maximum(), |connection| {
            if !current(shared, epoch) {
                return Err(io::ErrorKind::Interrupted.into());
            }
            if Instant::now() >= deadline {
                return Err(io::ErrorKind::TimedOut.into());
            }
            if refreshed.elapsed() >= Duration::from_secs(1) {
                let time = (context.time)().map_err(|_| io::ErrorKind::PermissionDenied)?;
                connection
                    .refresh_time(time)
                    .map_err(|_| io::ErrorKind::PermissionDenied)?;
                refreshed = Instant::now();
            }
            thread::park_timeout(resources.poll_interval);
            Ok(())
        });
        return match image {
            Ok(image) => {
                update.received(image);
                (storage, Ok(()))
            }
            Err(_) => (storage, Err(::tls::Error::Io)),
        };
    }
    let mut http = storage.retire();
    http.accept_with_prefix(connection, request_now, &prefix[..used]);
    let mut refreshed = Instant::now();
    let result = loop {
        if !current(shared, epoch) {
            break Ok(());
        }
        if refreshed.elapsed() >= Duration::from_secs(1) {
            let result =
                (context.time)().and_then(|time| http.transport_mut().unwrap().refresh_time(time));
            if let Err(error) = result {
                break Err(error);
            }
            refreshed = Instant::now();
        }
        if !current(shared, epoch) {
            break Ok(());
        }
        reject_busy(listener, counters);
        let packet = lock(shared).pending.take();
        let input = packet.as_ref().filter(|p| p.epoch == epoch);
        let frame = input.and_then(|p| {
            p.frame.map(|(sequence, capture)| Frame {
                sequence,
                capture,
                bytes: &p.bytes.as_ref()[..capture.len],
            })
        });
        let metadata = input.and_then(|p| {
            p.description
                .map(|(revision, length, content_type)| Metadata {
                    revision,
                    content_type,
                    bytes: &p.metadata[..length],
                })
        });
        let progress = http.poll_with_metadata(now(shared), frame, metadata);
        let mut state = lock(shared);
        if let Some(packet) = packet {
            state.returned = Some(packet);
        }
        publish(&mut state, &http, counters);
        state.progress.active = progress.active;
        state.progress.bytes_written = state
            .progress
            .bytes_written
            .saturating_add(progress.bytes_written);
        state.progress.frame_accepted |= progress.frame_accepted;
        state.progress.frames_completed = state
            .progress
            .frames_completed
            .saturating_add(progress.frames_completed);
        drop(state);
        if !progress.active {
            break Ok(());
        }
        thread::park_timeout(resources.poll_interval);
    };
    (http.retire(), result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initializer_error_or_unwind_cannot_strand_startup() {
        for unwind in [false, true] {
            let result = Worker::spawn::<fn() -> Result<::tls::TimeLease, ::tls::Error>>(
                TcpListener::bind("127.0.0.1:0").unwrap(),
                [0; 4],
                [0; 4],
                Options::default(),
                TlsOptions {
                    stack_bytes: 65536,
                    poll_interval: Duration::from_millis(1),
                },
                move || {
                    if unwind {
                        panic!("initializer witness");
                    }
                    Err(Error::Tls(::tls::Error::Unsupported))
                },
            );
            assert!(matches!(
                result,
                Err(Error::Worker) | Err(Error::Tls(::tls::Error::Unsupported))
            ));
        }
    }

    #[test]
    fn preempted_mailbox_does_not_block_capture_or_hide_network_transitions() {
        let shared = Arc::new(Shared {
            state: Mutex::new(State::<[u8; 4]> {
                update: None,
                pending: None,
                returned: None,
                clock: None,
                stream: ServerStats::default(),
                tls: TlsStats::default(),
                progress: Progress::default(),
                worker_now: 0,
            }),
            epoch: AtomicUsize::new(0),
            stop: AtomicBool::new(false),
            failed: AtomicBool::new(false),
        });
        let witness = shared.clone();
        let thread = thread::spawn(move || {
            while !witness.stop.load(Ordering::Acquire) {
                thread::park();
            }
        });
        let mut worker = Worker {
            shared: shared.clone(),
            thread: Some(thread),
            free: None,
            last_now: None,
            epoch: 0,
            stats: Default::default(),
            skipped_busy: 0,
        };
        let held = shared.state.lock().unwrap();
        worker.poll(10, true, None, None).unwrap();
        worker.poll(11, false, None, None).unwrap();
        worker.poll(12, true, None, None).unwrap();
        assert_eq!(shared.epoch.load(Ordering::Acquire), 3);
        assert_eq!(worker.stats().1.skipped_busy, 2);
        drop(held);
        worker.free = Some(Packet {
            bytes: [0; 4],
            frame: None,
            metadata: vec![0; 4].into_boxed_slice(),
            description: None,
            epoch: 0,
        });
        let mut bytes = [0xff, 0xd8, 0xff, 0xd9];
        let mut metadata = *b"data";
        let capture = Capture {
            format: camera::Format {
                width: 1,
                height: 1,
                pixels: camera::PixelFormat::Jpeg,
            },
            len: 4,
            timestamp_ms: 13,
        };
        worker
            .poll(
                13,
                true,
                Some(Frame {
                    sequence: 1,
                    capture,
                    bytes: &bytes,
                }),
                Some(Metadata {
                    revision: 7,
                    content_type: "application/test",
                    bytes: &metadata,
                }),
            )
            .unwrap();
        bytes.fill(0);
        metadata.fill(0);
        let packet = shared.state.lock().unwrap().pending.take().unwrap();
        assert_eq!(packet.bytes, [0xff, 0xd8, 0xff, 0xd9]);
        assert_eq!(&*packet.metadata, b"data");
        assert_eq!(packet.description, Some((7, 4, "application/test")));
        assert_eq!(packet.epoch, 3);
        assert!(matches!(
            worker.poll(12, true, None, None),
            Err(Error::Tls(::tls::Error::Time))
        ));
        assert!(worker.stopped());
        worker.stop().unwrap();
        assert!(matches!(
            worker.poll(13, true, None, None),
            Err(Error::Worker)
        ));
    }
}
