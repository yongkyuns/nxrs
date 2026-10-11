//! First-stage NuttX BLE provider: LE scanning only. Native scan results are
//! best-effort because NuttX buffers a finite number before SIOCBTSCANGET.
#![deny(unsafe_op_in_unsafe_fn)]
#[cfg(not(any(target_os = "nuttx", all(test, unix))))]
compile_error!("NuttX BLE provider requires NuttX; host admission is unit-test-only");

use ble_api::{
    Ble, Capabilities, Command, Completion, DeliveryError, Error, Event as ApiEvent,
    EventKind as ApiEventKind, EventSink, PeerId, ProviderStatus, Rejected, Request, SessionId,
    Uuid,
};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, TryLockError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

type ProviderEvent<const V: usize> = ApiEvent<V>;
type ProviderEventKind<const V: usize> = ApiEventKind<V>;

#[cfg(not(test))]
mod native;
#[cfg(test)]
mod tests;

const PEERS: usize = 32;
const BATCH: usize = 8;
const POLL: Duration = Duration::from_millis(50);
const DELIVERY_RETRY: Duration = Duration::from_millis(5);
#[cfg(not(test))]
const WORKER_STACK: usize = 8192;
#[cfg(test)]
const WORKER_STACK: usize = 128 * 1024;
static ACTIVE: AtomicBool = AtomicBool::new(false);
static NEXT_SESSION: AtomicU32 = AtomicU32::new(1);

/// NuttX scan state and callback storage are process-global.
#[derive(Debug)]
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

fn new_session() -> Result<SessionId, Error> {
    let value = NEXT_SESSION
        .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
            value.checked_add(1)
        })
        .map_err(|_| Error::Unavailable)?;
    SessionId::new(u64::from(value)).ok_or(Error::Unavailable)
}

struct Slot<const V: usize> {
    request: Option<Request<V>>,
    busy: bool,
}

struct Shared<const V: usize> {
    slot: Mutex<Slot<V>>,
    stopping: AtomicBool,
    stop_generation: AtomicU32,
    scan_active: AtomicBool,
    status: AtomicU32,
    fault: AtomicU32,
    lease: Mutex<Option<Lease>>,
}

impl<const V: usize> Shared<V> {
    fn new() -> Self {
        Self {
            slot: Mutex::new(Slot {
                request: None,
                busy: false,
            }),
            stopping: AtomicBool::new(false),
            stop_generation: AtomicU32::new(0),
            scan_active: AtomicBool::new(false),
            status: AtomicU32::new(0),
            fault: AtomicU32::new(0),
            lease: Mutex::new(None),
        }
    }
    fn status(&self) -> ProviderStatus {
        decode_status(self.status.load(Ordering::Acquire))
    }
    fn set_status(&self, status: ProviderStatus) {
        self.status.store(encode_status(status), Ordering::Release);
    }
    fn mark_ready(&self) -> bool {
        !self.stop_requested()
            && self
                .status
                .compare_exchange(
                    encode_status(ProviderStatus::Starting),
                    encode_status(ProviderStatus::Ready),
                    Ordering::AcqRel,
                    Ordering::Acquire,
                )
                .is_ok()
    }
    fn fault(&self) -> Option<Error> {
        let value = self.fault.load(Ordering::Acquire);
        (value != 0).then(|| error_from_code(value - 1))
    }
    fn stop_generation(&self) -> u32 {
        self.stop_generation.load(Ordering::Acquire)
    }
    fn stop_requested(&self) -> bool {
        self.stopping.load(Ordering::Acquire)
    }
    fn mark_stopping_unless_terminal(&self) {
        loop {
            let observed = self.status.load(Ordering::Acquire);
            if observed == encode_status(ProviderStatus::Stopped)
                || matches!(decode_status(observed), ProviderStatus::Failed(_))
            {
                return;
            }
            if self
                .status
                .compare_exchange_weak(
                    observed,
                    encode_status(ProviderStatus::Stopping),
                    Ordering::AcqRel,
                    Ordering::Acquire,
                )
                .is_ok()
            {
                return;
            }
        }
    }
}

pub struct NuttxBle<const V: usize> {
    session: SessionId,
    shared: Arc<Shared<V>>,
    worker: Option<JoinHandle<()>>,
    capabilities: Capabilities,
    finish_error: Option<Error>,
}

impl<const V: usize> NuttxBle<V> {
    /// Opens the bnep0 control socket on the provider worker and reports Ready
    /// after SIOCGBTINFO confirms the native ioctl path. An actual scan remains
    /// necessary to prove radio operation; that ioctl alone is only a snapshot.
    #[cfg(not(test))]
    pub fn open<S: EventSink<ProviderEvent<V>> + Send + 'static>(sink: S) -> Result<Self, Error> {
        let lease = Lease::acquire()?;
        Self::start(native::Native::new(), sink, Some(lease))
    }

    fn start<N: NativeScanner, S: EventSink<ProviderEvent<V>> + Send + 'static>(
        native: N,
        sink: S,
        lease: Option<Lease>,
    ) -> Result<Self, Error> {
        let session = new_session()?;
        let capabilities = Capabilities {
            central: false,
            scan: true,
            max_value_len: 0,
        };
        let shared = Arc::new(Shared::new());
        *shared.lease.lock().map_err(|_| Error::Io)? = lease;
        let worker_state = Arc::clone(&shared);
        let worker = thread::Builder::new()
            .name("ble-scan".into())
            .stack_size(WORKER_STACK)
            .spawn(move || {
                worker(native, sink, session, &worker_state);
            })
            .map_err(|_| Error::Unavailable)?;
        Ok(Self {
            session,
            shared,
            worker: Some(worker),
            capabilities,
            finish_error: None,
        })
    }
}

impl<const V: usize> Ble<V> for NuttxBle<V> {
    fn session(&self) -> SessionId {
        self.session
    }
    fn capabilities(&self) -> Capabilities {
        self.capabilities
    }
    fn status(&self) -> ProviderStatus {
        self.shared.status()
    }
    fn fault(&self) -> Option<Error> {
        self.finish_error.or_else(|| self.shared.fault())
    }
    fn submit(&mut self, request: Request<V>) -> Result<(), Rejected<Request<V>, Error>> {
        let reject = |error, value| Err(Rejected { value, error });
        if request.id.session != self.session {
            return reject(Error::InvalidValue, request);
        }
        let is_stop = matches!(request.command, Command::StopScan);
        match request.command {
            Command::StartScan { .. } if !self.capabilities.scan => {
                return reject(Error::Unsupported, request)
            }
            Command::StartScan { .. } | Command::StopScan => {}
            _ => return reject(Error::Unsupported, request),
        }
        let status = self.status();
        if status != ProviderStatus::Ready
            && !(is_stop
                && matches!(status, ProviderStatus::Failed(_))
                && self.shared.scan_active.load(Ordering::Acquire))
        {
            return reject(Error::Unavailable, request);
        }
        if is_stop && !self.shared.scan_active.load(Ordering::Acquire) {
            return reject(Error::Unavailable, request);
        }
        let mut slot = match self.shared.slot.try_lock() {
            Ok(slot) => slot,
            Err(TryLockError::WouldBlock) => return reject(Error::Busy, request),
            Err(TryLockError::Poisoned(_)) => return reject(Error::Io, request),
        };
        if self.shared.stop_requested() {
            return reject(Error::Unavailable, request);
        }
        if slot.busy || slot.request.is_some() {
            return reject(Error::Busy, request);
        }
        if matches!(request.command, Command::StartScan { .. })
            && self.shared.scan_active.load(Ordering::Acquire)
        {
            return reject(Error::Busy, request);
        }
        slot.busy = true;
        slot.request = Some(request);
        if let Some(worker) = &self.worker {
            worker.thread().unpark();
        }
        Ok(())
    }
    fn request_stop(&mut self) -> Result<(), Error> {
        if self.status() == ProviderStatus::Stopped {
            return Ok(());
        }
        if self
            .shared
            .stop_generation
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |generation| {
                generation.checked_add(1)
            })
            .is_err()
        {
            return Err(Error::Unavailable);
        }
        self.shared.stopping.store(true, Ordering::Release);
        self.shared.mark_stopping_unless_terminal();
        if let Some(worker) = &self.worker {
            worker.thread().unpark();
        }
        Ok(())
    }
    fn finish(&mut self) -> Result<(), Error> {
        if let Some(error) = self.finish_error {
            return Err(error);
        }
        if self.status() != ProviderStatus::Stopped {
            return Err(Error::Busy);
        }
        if let Some(worker) = self.worker.take() {
            if worker.join().is_err() {
                self.finish_error = Some(Error::Io);
                return Err(Error::Io);
            }
        }
        self.shared.lease.lock().map_err(|_| Error::Io)?.take();
        Ok(())
    }
}

impl<const V: usize> Drop for NuttxBle<V> {
    fn drop(&mut self) {
        if self.status() != ProviderStatus::Stopped {
            self.shared.stopping.store(true, Ordering::Release);
            let _ = self.shared.stop_generation.fetch_update(
                Ordering::AcqRel,
                Ordering::Acquire,
                |generation| generation.checked_add(1),
            );
            if let Some(worker) = &self.worker {
                worker.thread().unpark();
            }
        }
    }
}

trait NativeScanner: Send + 'static {
    fn check(&mut self) -> Result<(), Error>;
    fn start_scan(&mut self) -> Result<(), Error>;
    fn get(&mut self, records: &mut [Record]) -> Result<usize, Error>;
    fn stop_scan(&mut self) -> Result<(), Error>;
    fn close(&mut self);
}

/// Private fixed-width ABI. Native NuttX Bluetooth structs never cross into Rust.
#[repr(C)]
#[derive(Clone, Copy)]
struct Record {
    address: [u8; 6],
    address_type: u8,
    rssi: i8,
    length: u8,
    data: [u8; 64],
}

impl Default for Record {
    fn default() -> Self {
        Self {
            address: [0; 6],
            address_type: 0,
            rssi: 0,
            length: 0,
            data: [0; 64],
        }
    }
}

#[derive(Clone, Copy)]
struct Peer {
    address: [u8; 6],
    address_type: u8,
    id: PeerId,
    seen: bool,
}

fn worker<const V: usize, N: NativeScanner, S: EventSink<ProviderEvent<V>>>(
    mut native: N,
    mut sink: S,
    session: SessionId,
    state: &Shared<V>,
) {
    let event = |kind: ProviderEventKind<V>| ProviderEvent { session, kind };
    let mut peers: [Option<Peer>; PEERS] = [None; PEERS];
    let mut next_peer = 0u64;
    let mut active_filter = None;
    let mut scanning = false;
    if let Err(error) = native.check() {
        fail(&mut sink, state, &event, error);
    } else if state.mark_ready()
        && !emit(
            &mut sink,
            event(ProviderEventKind::Status(ProviderStatus::Ready)),
            state,
        )
        && !state.stop_requested()
    {
        fail(&mut sink, state, &event, Error::Io);
    }

    // Zero means every accepted stop request, including one made before this
    // worker reached startup, remains visible to the first loop iteration.
    let mut observed_stop = 0;
    loop {
        let stop_generation = state.stop_generation();
        if stop_generation != observed_stop {
            observed_stop = stop_generation;
            if scanning {
                match native.stop_scan() {
                    Ok(()) => {
                        state.scan_active.store(false, Ordering::Release);
                    }
                    Err(_) => {
                        fail(&mut sink, state, &event, Error::Io);
                        // A failed stop does not prove the static NuttX
                        // callback is quiet. Keep fd and lease; retry only on
                        // another explicit request_stop.
                        thread::park();
                        continue;
                    }
                }
            }
            break;
        }

        let request = match state.slot.lock() {
            Ok(mut slot) if !state.stop_requested() => slot.request.take(),
            _ => None,
        };
        if let Some(request) = request {
            match request.command {
                Command::StartScan { service } => match native.start_scan() {
                    Ok(()) => {
                        scanning = true;
                        active_filter = service;
                        state.scan_active.store(true, Ordering::Release);
                        for peer in peers.iter_mut().flatten() {
                            peer.seen = false;
                        }
                        state.slot.lock().unwrap().busy = false;
                        if !emit_start_completion(
                            &mut sink,
                            event(ProviderEventKind::Completed {
                                operation: request.id.operation,
                                result: Ok(Completion::ScanStarted),
                            }),
                            state,
                        ) && !state.stop_requested()
                        {
                            fail(&mut sink, state, &event, Error::Io);
                        }
                    }
                    Err(error) => {
                        state.slot.lock().unwrap().busy = false;
                        fail(&mut sink, state, &event, error);
                    }
                },
                Command::StopScan => {
                    let stopped = native.stop_scan();
                    if stopped.is_ok() {
                        scanning = false;
                        active_filter = None;
                        state.scan_active.store(false, Ordering::Release);
                        native.close();
                        state.slot.lock().unwrap().busy = false;
                        if !emit(
                            &mut sink,
                            event(ProviderEventKind::Completed {
                                operation: request.id.operation,
                                result: Ok(Completion::ScanStopped),
                            }),
                            state,
                        ) && !state.stop_requested()
                        {
                            fail(&mut sink, state, &event, Error::Io);
                        }
                    } else {
                        fail(&mut sink, state, &event, Error::Io);
                        thread::park();
                    }
                }
                _ => unreachable!("admission rejects non-scan commands"),
            }
            continue;
        }

        if matches!(state.status(), ProviderStatus::Failed(_)) {
            thread::park();
            continue;
        }

        if scanning {
            let mut records = [Record::default(); BATCH];
            match native.get(&mut records) {
                Ok(count) if count <= records.len() => {
                    for record in records.iter().take(count) {
                        if state.stop_requested() || has_stop_request(state) {
                            break;
                        }
                        if record.length > 64 {
                            fail(&mut sink, state, &event, Error::Io);
                            break;
                        }
                        if active_filter.is_some_and(|uuid| {
                            !advertisement_has_service(
                                &record.data[..usize::from(record.length)],
                                uuid,
                            )
                        }) {
                            continue;
                        }
                        let slot = peers.iter().position(|entry| {
                            entry.is_some_and(|peer| {
                                peer.address == record.address
                                    && peer.address_type == record.address_type
                            })
                        });
                        let index = match slot {
                            Some(index) => index,
                            None => {
                                let Some(index) = peers.iter().position(Option::is_none) else {
                                    fail(&mut sink, state, &event, Error::Io);
                                    break;
                                };
                                let Some(id) = next_peer.checked_add(1).map(PeerId) else {
                                    fail(&mut sink, state, &event, Error::Io);
                                    break;
                                };
                                next_peer = id.0;
                                peers[index] = Some(Peer {
                                    address: record.address,
                                    address_type: record.address_type,
                                    id,
                                    seen: false,
                                });
                                index
                            }
                        };
                        let peer = peers[index].as_mut().unwrap();
                        if peer.seen {
                            continue;
                        }
                        peer.seen = true;
                        if !emit(
                            &mut sink,
                            event(ProviderEventKind::Discovered {
                                peer: peer.id,
                                rssi_dbm: Some(i16::from(record.rssi)),
                            }),
                            state,
                        ) {
                            if !state.stop_requested() && !has_stop_request(state) {
                                fail(&mut sink, state, &event, Error::Io);
                            }
                            break;
                        }
                    }
                }
                Ok(_) => fail(&mut sink, state, &event, Error::Io),
                Err(error) => fail(&mut sink, state, &event, error),
            }
            if !state.stop_requested() {
                thread::park_timeout(POLL);
            }
            continue;
        }
        thread::park();
    }

    native.close();
    state.scan_active.store(false, Ordering::Release);
    if let Ok(mut slot) = state.slot.lock() {
        slot.request = None;
        slot.busy = false;
    }
    state.set_status(ProviderStatus::Stopped);
    let _ = sink.try_emit(event(ProviderEventKind::Status(ProviderStatus::Stopped)));
}

fn fail<const V: usize, S: EventSink<ProviderEvent<V>>>(
    sink: &mut S,
    state: &Shared<V>,
    event: &impl Fn(ProviderEventKind<V>) -> ProviderEvent<V>,
    error: Error,
) {
    let _ = state
        .fault
        .compare_exchange(0, error as u32 + 1, Ordering::AcqRel, Ordering::Acquire);
    state.set_status(ProviderStatus::Failed(error));
    let _ = emit(
        sink,
        event(ProviderEventKind::Status(ProviderStatus::Failed(error))),
        state,
    );
}

fn has_stop_request<const V: usize>(state: &Shared<V>) -> bool {
    state
        .slot
        .lock()
        .ok()
        .and_then(|slot| slot.request)
        .is_some_and(|request| matches!(request.command, Command::StopScan))
}

fn emit<const V: usize, S: EventSink<ProviderEvent<V>>>(
    sink: &mut S,
    mut event: ProviderEvent<V>,
    state: &Shared<V>,
) -> bool {
    loop {
        if state.stop_requested() || has_stop_request(state) {
            return false;
        }
        match sink.try_emit(event) {
            Ok(()) => return true,
            Err(rejected) if rejected.error == DeliveryError::Full => {
                event = rejected.value;
                thread::park_timeout(DELIVERY_RETRY);
            }
            Err(_) => return false,
        }
    }
}

/// Publish ScanStarted before honoring a queued StopScan. This lets a caller
/// react to the completion immediately without losing its start result.
fn emit_start_completion<const V: usize, S: EventSink<ProviderEvent<V>>>(
    sink: &mut S,
    mut event: ProviderEvent<V>,
    state: &Shared<V>,
) -> bool {
    loop {
        if state.stop_requested() {
            return false;
        }
        match sink.try_emit(event) {
            Ok(()) => return true,
            Err(rejected) if rejected.error == DeliveryError::Full => {
                event = rejected.value;
                thread::park_timeout(DELIVERY_RETRY);
            }
            Err(_) => return false,
        }
    }
}

fn advertisement_has_service(data: &[u8], requested: Uuid) -> bool {
    let mut offset = 0;
    while offset < data.len() {
        let field_len = usize::from(data[offset]);
        if field_len == 0 {
            return false;
        }
        let Some(end) = offset.checked_add(field_len + 1) else {
            return false;
        };
        if end > data.len() || field_len < 1 {
            return false;
        }
        let kind = data[offset + 1];
        let values = &data[offset + 2..end];
        let item_width = match kind {
            0x02 | 0x03 => 2,
            0x04 | 0x05 => 4,
            0x06 | 0x07 => 16,
            _ => {
                offset = end;
                continue;
            }
        };
        if values.len().is_multiple_of(item_width) {
            for value in values.chunks_exact(item_width) {
                if list_uuid(value, item_width) == Some(requested) {
                    return true;
                }
            }
        }
        offset = end;
    }
    false
}

fn list_uuid(value: &[u8], width: usize) -> Option<Uuid> {
    let mut canonical = [0; 16];
    match width {
        2 => {
            canonical[2..4].copy_from_slice(&[value[1], value[0]]);
            canonical[4..].copy_from_slice(&[
                0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0x80, 0x5f, 0x9b, 0x34, 0xfb,
            ]);
        }
        4 => {
            canonical[0..4].copy_from_slice(&[value[3], value[2], value[1], value[0]]);
            canonical[4..].copy_from_slice(&[
                0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0x80, 0x5f, 0x9b, 0x34, 0xfb,
            ]);
        }
        16 => {
            for (dst, src) in canonical.iter_mut().zip(value.iter().rev()) {
                *dst = *src;
            }
        }
        _ => return None,
    }
    Some(Uuid(canonical))
}

fn encode_status(status: ProviderStatus) -> u32 {
    match status {
        ProviderStatus::Starting => 0,
        ProviderStatus::Ready => 1,
        ProviderStatus::Stopping => 2,
        ProviderStatus::Stopped => 3,
        ProviderStatus::Failed(error) => 16 + error as u32,
    }
}

fn decode_status(status: u32) -> ProviderStatus {
    match status {
        0 => ProviderStatus::Starting,
        1 => ProviderStatus::Ready,
        2 => ProviderStatus::Stopping,
        3 => ProviderStatus::Stopped,
        value => ProviderStatus::Failed(error_from_code(value.saturating_sub(16))),
    }
}

fn error_from_code(code: u32) -> Error {
    match code {
        0 => Error::Unsupported,
        1 => Error::Busy,
        2 => Error::Unavailable,
        3 => Error::PermissionDenied,
        4 => Error::InvalidPeer,
        5 => Error::InvalidValue,
        6 => Error::AuthenticationRequired,
        7 => Error::Disconnected,
        8 => Error::Timeout,
        _ => Error::Io,
    }
}
