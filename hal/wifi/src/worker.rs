//! Private std execution adapter; drivers supply normalized radio operations.
#![forbid(unsafe_code)]

use crate::*;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, TryLockError};
use std::thread::{self, JoinHandle};
use std::time::Duration;
#[cfg(test)]
mod tests;

const DELIVERY_RETRY: Duration = Duration::from_millis(5);
const LINK_POLL: Duration = Duration::from_millis(500);
const CLEANUP_RETRY: Duration = Duration::from_millis(100);
const CLEANUP_RETRY_MAX: Duration = Duration::from_secs(30);
static NEXT_SESSION: AtomicU32 = AtomicU32::new(1);
static ACTIVE: AtomicBool = AtomicBool::new(false);

/// One process-local interface owner, retained through the final native call.
pub(crate) struct Lease;
impl Lease {
    pub(crate) fn acquire() -> Result<Self, Error> {
        ACTIVE
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| Self)
            .map_err(|_| Error::Busy)
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        ACTIVE.store(false, Ordering::Release);
    }
}

fn session() -> Result<SessionId, Error> {
    let value = NEXT_SESSION
        .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
            value.checked_add(1)
        })
        .map_err(|_| Error::Unavailable)?;
    SessionId::new(u64::from(value)).ok_or(Error::Unavailable)
}

struct Pending {
    request: Option<Request>,
    busy: bool,
}

struct Shared {
    pending: Mutex<Pending>,
    stopping: AtomicBool,
    stop_generation: AtomicU32,
    abandoned: AtomicBool,
    status: AtomicU32,
    fault: AtomicU32,
}

impl Shared {
    fn new() -> Self {
        Self {
            pending: Mutex::new(Pending {
                request: None,
                busy: false,
            }),
            stopping: AtomicBool::new(false),
            stop_generation: AtomicU32::new(0),
            abandoned: AtomicBool::new(false),
            status: AtomicU32::new(0),
            fault: AtomicU32::new(0),
        }
    }
    fn stopping(&self) -> bool {
        self.stopping.load(Ordering::Acquire)
    }
    fn set_status(&self, status: ProviderStatus) {
        if matches!(status, ProviderStatus::Failed(_)) {
            let _ = self.fault.compare_exchange(
                0,
                encode_status(status),
                Ordering::AcqRel,
                Ordering::Acquire,
            );
        }
        self.status.store(encode_status(status), Ordering::Release);
    }
}

pub(crate) struct Worker {
    session: SessionId,
    shared: Arc<Shared>,
    worker: Option<JoinHandle<()>>,
    finish_error: Option<Error>,
    capabilities: Capabilities,
}

impl Worker {
    #[cfg(test)]
    pub(crate) fn start<N: Driver, S: EventSink<Event> + Send + 'static, G: Send + 'static>(
        native: N,
        sink: S,
        guard: G,
    ) -> Result<Self, Error> {
        Self::start_with_stack(native, sink, guard, 128 * 1024)
    }

    pub(crate) fn start_with_stack<
        N: Driver,
        S: EventSink<Event> + Send + 'static,
        G: Send + 'static,
    >(
        native: N,
        sink: S,
        guard: G,
        stack_bytes: usize,
    ) -> Result<Self, Error> {
        let session = session()?;
        let capabilities = native.capabilities();
        let shared = Arc::new(Shared::new());
        let state = Arc::clone(&shared);
        let worker = thread::Builder::new()
            .name("wifi".into())
            .stack_size(stack_bytes)
            .spawn(move || {
                // The worker holds the interface lease through its final native
                // call even if the public handle is dropped without joining.
                let _guard = guard;
                worker(native, sink, session, &state);
            })
            .map_err(|_| Error::Unavailable)?;
        Ok(Self {
            session,
            shared,
            worker: Some(worker),
            finish_error: None,
            capabilities,
        })
    }
}

impl Wifi for Worker {
    fn session(&self) -> SessionId {
        self.session
    }
    fn capabilities(&self) -> Capabilities {
        self.capabilities
    }
    fn status(&self) -> ProviderStatus {
        decode_status(self.shared.status.load(Ordering::Acquire))
    }
    fn fault(&self) -> Option<Error> {
        match decode_status(self.shared.fault.load(Ordering::Acquire)) {
            ProviderStatus::Failed(error) => Some(error),
            _ => self.finish_error,
        }
    }
    fn submit(&mut self, request: Request) -> Result<(), Rejected<Command, Error>> {
        let reject = |error, request: Request| {
            Err(Rejected {
                value: request.command,
                error,
            })
        };
        if request.id.session != self.session {
            return reject(Error::InvalidConfig, request);
        }
        let supported = match &request.command {
            Command::Scan => self.capabilities.scan,
            Command::Connect {
                credentials: Credentials::Open,
                ..
            } => self.capabilities.open_network,
            Command::Connect {
                credentials: Credentials::Wpa2Personal(_),
                ..
            } => self.capabilities.wpa2_personal,
            Command::Disconnect => {
                self.capabilities.open_network || self.capabilities.wpa2_personal
            }
        };
        if !supported {
            return reject(Error::Unsupported, request);
        }
        if self.status() != ProviderStatus::Ready {
            return reject(Error::Unavailable, request);
        }
        // Admission cannot wait for a native operation or another sender.
        let mut state = match self.shared.pending.try_lock() {
            Ok(state) => state,
            Err(TryLockError::WouldBlock) => return reject(Error::Busy, request),
            Err(TryLockError::Poisoned(_)) => return reject(Error::Io, request),
        };
        if self.shared.stopping() {
            return reject(Error::Unavailable, request);
        }
        if state.busy {
            return reject(Error::Busy, request);
        }
        state.busy = true;
        state.request = Some(request);
        if let Some(worker) = &self.worker {
            worker.thread().unpark();
        }
        Ok(())
    }
    fn request_stop(&mut self) -> Result<(), Error> {
        if self.status() == ProviderStatus::Stopped {
            return Ok(());
        }
        self.shared
            .stop_generation
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |value| {
                value.checked_add(1)
            })
            .map_err(|_| Error::Unavailable)?;
        self.shared.stopping.store(true, Ordering::Release);
        // Do not overwrite a concurrent terminal publication.
        let _ = self
            .shared
            .status
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |value| {
                if value == 3 {
                    None
                } else {
                    Some(2)
                }
            });
        if let Some(worker) = &self.worker {
            worker.thread().unpark();
        }
        Ok(())
    }
    fn finish(&mut self) -> Result<(), Error> {
        if self.status() != ProviderStatus::Stopped {
            return Err(Error::Busy);
        }
        if let Some(error) = self.finish_error {
            return Err(error);
        }
        if let Some(worker) = self.worker.take() {
            if worker.join().is_err() {
                self.finish_error = Some(Error::Io);
                return Err(Error::Io);
            }
        }
        Ok(())
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        // No blocking native join in Drop. The detached worker still owns its
        // socket, buffer and lease; explicit finish is the normal lifecycle.
        self.shared.abandoned.store(true, Ordering::Release);
        self.shared.stopping.store(true, Ordering::Release);
        if let Some(worker) = &self.worker {
            worker.thread().unpark();
        }
    }
}

pub(crate) trait Driver: Send + 'static {
    fn capabilities(&self) -> Capabilities;
    /// Initialize blocking SDK/IPC resources on this worker, before Ready.
    fn check(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Calls the worker's admission closure, never a consuming service inline.
    /// False means delivery stopped; no successful completion may follow it.
    fn scan(&mut self, emit: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error>;
    // Connect errors eligible for retry must mean native rollback completed.
    // Unknown cleanup/disconnect state must return Io/Protocol and fault.
    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error>;
    fn disconnect(&mut self) -> Result<(), Error>;
    fn link(&mut self) -> Result<LinkState, Error>;
    /// Fallible teardown. Retain the driver and guard until cleanup succeeds.
    fn close(&mut self) -> Result<(), Error>;
}

fn emit<S: EventSink<Event>>(sink: &mut S, mut event: Event, state: &Shared) -> bool {
    loop {
        if state.stopping() {
            return false;
        }
        match sink.try_emit(event) {
            Ok(()) => return true,
            Err(rejected) if rejected.error == DeliveryError::Full => {
                event = rejected.value;
                // EventSink has no capacity-notification API. Retain one item
                // and retry here; this timer never enters the service loop.
                thread::park_timeout(DELIVERY_RETRY);
            }
            Err(_) => return false,
        }
    }
}

fn worker<N: Driver, S: EventSink<Event>>(
    mut native: N,
    mut sink: S,
    session: SessionId,
    state: &Shared,
) {
    let event = |kind| Event { session, kind };
    let mut link = LinkState::Unknown;
    let initialized = !state.stopping()
        && match native.check() {
            Ok(()) => {
                let _ = state
                    .status
                    .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire);
                true
            }
            Err(error) => {
                state.set_status(ProviderStatus::Failed(error));
                let _ = emit(
                    &mut sink,
                    event(EventKind::Status(ProviderStatus::Failed(error))),
                    state,
                );
                false
            }
        };
    if initialized
        && emit(
            &mut sink,
            event(EventKind::Status(ProviderStatus::Ready)),
            state,
        )
    {
        loop {
            let request = {
                if state.stopping() {
                    break;
                }
                let request = state.pending.lock().unwrap().request.take();
                match request {
                    Some(request) => request,
                    None => {
                        // std park retains an unpark token even if admission or
                        // stop happened just before this wait; no lost wakeup.
                        match native.link() {
                            Ok(observed) if observed != link => {
                                link = observed;
                                if !emit(&mut sink, event(EventKind::LinkChanged(link)), state) {
                                    break;
                                }
                            }
                            Err(error) => {
                                state.set_status(ProviderStatus::Failed(error));
                                let _ = emit(
                                    &mut sink,
                                    event(EventKind::Status(ProviderStatus::Failed(error))),
                                    state,
                                );
                                while !state.stopping() {
                                    thread::park();
                                }
                                break;
                            }
                            _ => {}
                        }
                        thread::park_timeout(LINK_POLL);
                        continue;
                    }
                }
            };
            let scanning = matches!(request.command, Command::Scan);
            let result = match request.command {
                Command::Scan => scan(&mut native, &mut sink, session, request.id.operation, state)
                    .map(|reported| {
                        reported.map(|reported| Completion::Scan {
                            reported,
                            truncated: true,
                        })
                    }),
                Command::Connect { ssid, credentials } => native
                    .connect(&ssid, &credentials)
                    .map(|()| Some(Completion::Connected)),
                Command::Disconnect => native.disconnect().map(|()| Some(Completion::Disconnected)),
            };
            if state.stopping() {
                break;
            }
            match result {
                Ok(Some(completion)) => {
                    if !scanning {
                        match native.link() {
                            Ok(observed) => {
                                link = observed;
                                if !emit(&mut sink, event(EventKind::LinkChanged(link)), state) {
                                    break;
                                }
                            }
                            Err(error) => {
                                state.set_status(ProviderStatus::Failed(error));
                                let _ = emit(
                                    &mut sink,
                                    event(EventKind::Status(ProviderStatus::Failed(error))),
                                    state,
                                );
                                while !state.stopping() {
                                    thread::park();
                                }
                                break;
                            }
                        }
                    }
                    state.pending.lock().unwrap().busy = false;
                    if !emit(
                        &mut sink,
                        event(EventKind::Completed {
                            operation: request.id.operation,
                            result: Ok(completion),
                        }),
                        state,
                    ) {
                        break;
                    }
                }
                Ok(None) => break, // closed delivery or stop, never a false success
                Err(error) => {
                    // A rejected association or timed-out DHCP attempt is a
                    // completed operation only if native cleanup proved idle.
                    if !scanning
                        && matches!(
                            error,
                            Error::Timeout
                                | Error::AuthenticationFailed
                                | Error::Busy
                                | Error::InvalidConfig
                                | Error::Unsupported
                        )
                    {
                        state.pending.lock().unwrap().busy = false;
                        if !emit(
                            &mut sink,
                            event(EventKind::Completed {
                                operation: request.id.operation,
                                result: Err(error),
                            }),
                            state,
                        ) {
                            break;
                        }
                        continue;
                    }
                    // A failed blocking scan cannot prove the driver is idle.
                    // Close admission and require an explicit session stop.
                    state.set_status(ProviderStatus::Failed(error));
                    if !emit(
                        &mut sink,
                        event(EventKind::Status(ProviderStatus::Failed(error))),
                        state,
                    ) {
                        break;
                    }
                    while !state.stopping() {
                        thread::park();
                    }
                    break;
                }
            }
        }
    }
    // A cleanup failure is not quiescent teardown. A live owner can retry stop;
    // an abandoned worker retains ownership and retries with bounded backoff.
    let mut retry_delay = CLEANUP_RETRY;
    loop {
        let generation = state.stop_generation.load(Ordering::Acquire);
        match native.close() {
            Ok(()) => break,
            Err(error) => {
                state.set_status(ProviderStatus::Failed(error));
                let _ = sink.try_emit(event(EventKind::Status(ProviderStatus::Failed(error))));
                while !state.abandoned.load(Ordering::Acquire)
                    && state.stop_generation.load(Ordering::Acquire) == generation
                {
                    thread::park();
                }
                if state.abandoned.load(Ordering::Acquire) {
                    // No owner remains to interrupt teardown; stale unpark
                    // tokens must not turn native failures into a retry spin.
                    thread::sleep(retry_delay);
                    retry_delay = retry_delay.saturating_mul(2).min(CLEANUP_RETRY_MAX);
                }
            }
        }
    }
    // No future source calls after Stopped. Drop native resources first.
    drop(native);
    let mut pending = state.pending.lock().unwrap();
    state.stopping.store(true, Ordering::Release);
    pending.request = None;
    pending.busy = false;
    state.set_status(ProviderStatus::Stopped);
    // Terminal snapshot + one wakeup. A full queue already has a wakeup; the
    // owner reconciles sticky state between dispatches, so no retry is needed.
    let _ = sink.try_emit(event(EventKind::Status(ProviderStatus::Stopped)));
    drop(pending);
}

fn scan<N: Driver, S: EventSink<Event>>(
    native: &mut N,
    sink: &mut S,
    session: SessionId,
    operation: OperationId,
    state: &Shared,
) -> Result<Option<u32>, Error> {
    if state.stopping() {
        return Ok(None);
    }
    let mut reported = 0;
    let complete = native.scan(&mut |point| {
        if emit(
            sink,
            Event {
                session,
                kind: EventKind::ScanResult {
                    operation,
                    access_point: point,
                },
            },
            state,
        ) {
            reported += 1;
            true
        } else {
            false
        }
    })?;
    Ok(complete.then_some(reported))
}

fn encode_status(status: ProviderStatus) -> u32 {
    match status {
        ProviderStatus::Starting => 0,
        ProviderStatus::Ready => 1,
        ProviderStatus::Stopping => 2,
        ProviderStatus::Stopped => 3,
        ProviderStatus::Failed(error) => match error {
            Error::Unsupported => 4,
            Error::Busy => 5,
            Error::Unavailable => 6,
            Error::PermissionDenied => 7,
            Error::AuthenticationFailed => 8,
            Error::InvalidConfig => 9,
            Error::Timeout => 10,
            Error::Protocol => 11,
            Error::Io => 12,
        },
    }
}
fn decode_status(value: u32) -> ProviderStatus {
    match value {
        0 => ProviderStatus::Starting,
        1 => ProviderStatus::Ready,
        2 => ProviderStatus::Stopping,
        3 => ProviderStatus::Stopped,
        _ => ProviderStatus::Failed(match value - 4 {
            0 => Error::Unsupported,
            1 => Error::Busy,
            2 => Error::Unavailable,
            3 => Error::PermissionDenied,
            4 => Error::AuthenticationFailed,
            5 => Error::InvalidConfig,
            6 => Error::Timeout,
            7 => Error::Protocol,
            _ => Error::Io,
        }),
    }
}
