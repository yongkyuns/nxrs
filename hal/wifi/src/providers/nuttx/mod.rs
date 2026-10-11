//! NuttX station provider. Native waits and bounded delivery live on one
//! worker, not the application dispatcher. Capabilities reflect the build.
#![deny(unsafe_op_in_unsafe_fn)]

use crate::*;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, TryLockError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use std::vec;

#[cfg(not(test))]
mod native;
#[cfg(test)]
mod tests;

const SCAN_BYTES: usize = 4096;
const DELIVERY_RETRY: Duration = Duration::from_millis(5);
const LINK_POLL: Duration = Duration::from_millis(500);
const SCAN_READ_TIMEOUT: Duration = Duration::from_secs(6);
const SCAN_READ_POLL: Duration = Duration::from_millis(20);
#[cfg(not(test))]
const WORKER_STACK: usize = 8192;
// Host debug fixtures exercise unoptimized std/channel assertion paths, not
// the target's release stack budget (which still needs on-device qualification).
#[cfg(test)]
const WORKER_STACK: usize = 128 * 1024;
static ACTIVE: AtomicBool = AtomicBool::new(false);
static NEXT_SESSION: AtomicU32 = AtomicU32::new(1);

/// Wireless ioctl providers may block (ESP) or return EAGAIN while scanning
/// (BCMF). Retry reads only, never restart an accepted scan. Native calls remain
/// non-preemptible, so this deadline bounds retries, not an individual ioctl.
fn read_scan_with_retry(
    mut read: impl FnMut() -> Result<usize, Error>,
    timeout: Duration,
) -> Result<usize, Error> {
    let deadline = Instant::now() + timeout;
    loop {
        match read() {
            Err(Error::Busy) => {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Err(Error::Timeout);
                }
                thread::sleep(SCAN_READ_POLL.min(remaining));
            }
            result => return result,
        }
    }
}

/// One process-local owner: NuttX's scan storage is shared by interface.
struct Lease;
impl Lease {
    fn acquire() -> Result<Self, Error> {
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
    status: AtomicU32,
}

impl Shared {
    fn new() -> Self {
        Self {
            pending: Mutex::new(Pending {
                request: None,
                busy: false,
            }),
            stopping: AtomicBool::new(false),
            status: AtomicU32::new(0),
        }
    }
    fn stopping(&self) -> bool {
        self.stopping.load(Ordering::Acquire)
    }
    fn set_status(&self, status: ProviderStatus) {
        self.status.store(encode_status(status), Ordering::Release);
    }
}

pub struct NuttxWifi {
    session: SessionId,
    shared: Arc<Shared>,
    worker: Option<JoinHandle<()>>,
    finish_error: Option<Error>,
    capabilities: Capabilities,
}

impl NuttxWifi {
    /// The selected interface must already be up. Acquisition alone does not
    /// switch networks, enable the adapter, or persist credentials.
    #[cfg(not(test))]
    pub fn open<S: EventSink<Event> + Send + 'static>(sink: S) -> Result<Self, Error> {
        let lease = Lease::acquire()?;
        let native = native::Native::open()?;
        Self::start(native, sink, Some(lease))
    }

    fn start<N: NativeWifi, S: EventSink<Event> + Send + 'static>(
        native: N,
        sink: S,
        lease: Option<Lease>,
    ) -> Result<Self, Error> {
        let session = session()?;
        let capabilities = native.capabilities();
        let shared = Arc::new(Shared::new());
        let state = Arc::clone(&shared);
        let worker = thread::Builder::new()
            .name("wifi".into())
            .stack_size(WORKER_STACK)
            .spawn(move || {
                // The worker holds the interface lease through its final native
                // call even if the public handle is dropped without joining.
                let _lease = lease;
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

impl Wifi for NuttxWifi {
    fn session(&self) -> SessionId {
        self.session
    }
    fn capabilities(&self) -> Capabilities {
        self.capabilities
    }
    fn status(&self) -> ProviderStatus {
        decode_status(self.shared.status.load(Ordering::Acquire))
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

impl Drop for NuttxWifi {
    fn drop(&mut self) {
        // No blocking native join in Drop. The detached worker still owns its
        // socket, buffer and lease; explicit finish is the normal lifecycle.
        self.shared.stopping.store(true, Ordering::Release);
        if let Some(worker) = &self.worker {
            worker.thread().unpark();
        }
    }
}

pub trait NativeWifi: Send + 'static {
    fn capabilities(&self) -> Capabilities;
    fn scan(&mut self, buffer: &mut [u8]) -> Result<usize, Error>;
    fn next(&self, buffer: &[u8], offset: &mut usize) -> Result<Option<Field>, Error>;
    // Connect errors eligible for retry must mean native rollback completed.
    // Unknown cleanup/disconnect state must return Io/Protocol and fault.
    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error>;
    fn disconnect(&mut self) -> Result<(), Error>;
    fn link(&mut self) -> Result<LinkState, Error>;
}

/// Private fixed-width C ABI; only target-header layout decoding lives in C.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct Field {
    pub kind: u32,
    pub flags: u32,
    pub length: u32,
    pub data: [u8; 32],
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

fn worker<N: NativeWifi, S: EventSink<Event>>(
    mut native: N,
    mut sink: S,
    session: SessionId,
    state: &Shared,
) {
    let mut buffer = vec![0u8; SCAN_BYTES].into_boxed_slice();
    let _ = state
        .status
        .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire);
    let event = |kind| Event { session, kind };
    let mut link = LinkState::Unknown;
    if emit(
        &mut sink,
        event(EventKind::Status(ProviderStatus::Ready)),
        state,
    ) {
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
                Command::Scan => scan(
                    &mut native,
                    &mut sink,
                    session,
                    request.id.operation,
                    state,
                    &mut buffer,
                )
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
    // No future source calls after Stopped. Drop native resources first.
    drop(native);
    drop(buffer);
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

fn scan<N: NativeWifi, S: EventSink<Event>>(
    native: &mut N,
    sink: &mut S,
    session: SessionId,
    operation: OperationId,
    state: &Shared,
    buffer: &mut [u8],
) -> Result<Option<u32>, Error> {
    if state.stopping() {
        return Ok(None);
    }
    let length = native.scan(buffer)?;
    let data = buffer.get(..length).ok_or(Error::Protocol)?;
    let mut offset = 0;
    let mut point = None;
    let mut reported = 0;
    let deliver = |point, sink: &mut S, reported: &mut u32| {
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
            *reported += 1;
            true
        } else {
            false
        }
    };
    while offset < data.len() {
        let previous = offset;
        let field = native.next(data, &mut offset)?.ok_or(Error::Protocol)?;
        if offset <= previous || offset > data.len() {
            return Err(Error::Protocol);
        }
        if field.kind == 1 {
            if let Some(point) = point.take() {
                if !deliver(point, sink, &mut reported) {
                    return Ok(None);
                }
            }
            point = Some(AccessPoint {
                ssid: None,
                security: Security::Other,
                rssi_dbm: None,
            });
        } else if let Some(point) = point.as_mut() {
            match field.kind {
                2 => {
                    let ssid = field
                        .data
                        .get(..field.length as usize)
                        .ok_or(Error::Protocol)?;
                    point.ssid = if ssid.is_empty() {
                        None
                    } else {
                        Some(Ssid::new(ssid).map_err(|_| Error::Protocol)?)
                    };
                }
                3 => {
                    point.rssi_dbm = if field.flags & 3 == 3 {
                        Some(i16::from(field.data[0] as i8))
                    } else {
                        None
                    }
                }
                4 => {
                    point.security = if field.flags & 1 != 0 {
                        Security::Open
                    } else {
                        Security::Other
                    }
                }
                _ => {}
            }
        } else if field.kind != 0 {
            return Err(Error::Protocol);
        }
    }
    if let Some(point) = point {
        if !deliver(point, sink, &mut reported) {
            return Ok(None);
        }
    }
    Ok(Some(reported))
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
