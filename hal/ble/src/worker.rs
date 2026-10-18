//! Private, portable BLE scan worker. Native adapters supply normalized reports.
#![forbid(unsafe_code)]

use crate::{
    Ble, Capabilities, Command, Completion, DeliveryError, Error, Event, EventKind, EventSink,
    PeerId, ProviderStatus, Rejected, Request, SessionId, Uuid,
};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

static NEXT_SESSION: AtomicU32 = AtomicU32::new(1);
#[cfg(all(target_os = "nuttx", not(test)))]
const WORKER_STACK: usize = 8192;
#[cfg(any(test, not(target_os = "nuttx")))]
const WORKER_STACK: usize = 128 * 1024;

static ACTIVE: AtomicBool = AtomicBool::new(false);

/// Process-global ownership for native BLE callback storage.
#[derive(Debug)]
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

pub(crate) fn new_session() -> Result<SessionId, Error> {
    let value = NEXT_SESSION
        .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
            value.checked_add(1)
        })
        .map_err(|_| Error::Unavailable)?;
    SessionId::new(u64::from(value)).ok_or(Error::Unavailable)
}

pub(crate) struct Worker {
    session: SessionId,
    shared: Arc<Shared>,
    worker: Option<JoinHandle<()>>,
    finish_error: Option<Error>,
}

impl Worker {
    pub(crate) fn start<N: Scanner, S: EventSink<Event> + Send + 'static, G: Send + 'static>(
        native: N,
        sink: S,
        guard: G,
    ) -> Result<Self, Error> {
        let session = new_session()?;
        let shared = Arc::new(Shared::new());
        let worker_state = Arc::clone(&shared);
        let handle = thread::Builder::new()
            .name("ble-scan".into())
            .stack_size(WORKER_STACK)
            .spawn(move || {
                run(native, sink, session, worker_state, QuietGuard(Some(guard)));
            })
            .map_err(|_| Error::Unavailable)?;
        Ok(Self {
            session,
            shared,
            worker: Some(handle),
            finish_error: None,
        })
    }
}

impl Ble for Worker {
    fn session(&self) -> SessionId {
        self.session
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities { scan: true }
    }
    fn status(&self) -> ProviderStatus {
        self.shared.status()
    }
    fn fault(&self) -> Option<Error> {
        self.shared.fault().or(self.finish_error)
    }
    fn submit(&mut self, request: Request) -> Result<(), Rejected<Request, Error>> {
        let reject = |error, value| Err(Rejected { value, error });
        if request.id.session != self.session {
            return reject(Error::InvalidValue, request);
        }
        let is_stop = matches!(request.command, Command::StopScan);
        if self.status() != ProviderStatus::Ready
            && !(is_stop
                && matches!(self.status(), ProviderStatus::Failed(_))
                && self.shared.scan_active.load(Ordering::Acquire))
        {
            return reject(Error::Unavailable, request);
        }
        if is_stop && !self.shared.scan_active.load(Ordering::Acquire) {
            return reject(Error::Unavailable, request);
        }
        let mut slot = match self.shared.slot.try_lock() {
            Ok(slot) => slot,
            Err(std::sync::TryLockError::WouldBlock) => return reject(Error::Busy, request),
            Err(std::sync::TryLockError::Poisoned(_)) => return reject(Error::Io, request),
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
        self.shared
            .stop_generation
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |v| v.checked_add(1))
            .map_err(|_| Error::Unavailable)?;
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
        Ok(())
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        if self.status() != ProviderStatus::Stopped {
            self.shared.abandoned.store(true, Ordering::Release);
            self.shared.stopping.store(true, Ordering::Release);
            let _ = self.shared.stop_generation.fetch_update(
                Ordering::AcqRel,
                Ordering::Acquire,
                |v| v.checked_add(1),
            );
            if let Some(worker) = &self.worker {
                worker.thread().unpark();
            }
        }
    }
}

const PEERS: usize = 32;
const BATCH: usize = 8;
const POLL: Duration = Duration::from_millis(50);
const DELIVERY_RETRY: Duration = Duration::from_millis(5);
const STOP_RETRY_INITIAL: Duration = Duration::from_millis(250);
const STOP_RETRY_MAX: Duration = Duration::from_secs(30);

pub(super) trait Scanner: Send + 'static {
    /// Opaque adapter-local identity; desktop stacks need not expose MAC addresses.
    type Peer: Copy + Default + Eq + Send;
    fn check(&mut self) -> Result<(), Error>;
    fn bind_session(&mut self, _session: SessionId) -> Result<(), Error> {
        Ok(())
    }
    fn configure_filter(&mut self, _service: Option<Uuid>) -> Result<(), Error> {
        Ok(())
    }
    fn remember_peer(
        &mut self,
        _session: SessionId,
        _id: PeerId,
        _peer: Self::Peer,
    ) -> Result<(), Error> {
        Ok(())
    }
    fn start_scan(&mut self) -> Result<(), Error>;
    fn get(
        &mut self,
        service: Option<Uuid>,
        output: &mut [ScanReport<Self::Peer>],
    ) -> Result<usize, Error>;
    fn stop_scan(&mut self) -> Result<(), Error>;
    fn close(&mut self);
}

#[derive(Clone, Copy, Debug, Default)]
pub(super) struct ScanReport<K> {
    pub peer: K,
    pub rssi_dbm: Option<i16>,
    pub matches_filter: bool,
}

pub(super) struct Slot {
    pub request: Option<Request>,
    pub busy: bool,
}

pub(super) struct Shared {
    pub slot: Mutex<Slot>,
    pub stopping: AtomicBool,
    pub abandoned: AtomicBool,
    pub stop_generation: AtomicU32,
    pub scan_active: AtomicBool,
    status: AtomicU32,
    fault: AtomicU32,
}

impl Shared {
    pub fn new() -> Self {
        Self {
            slot: Mutex::new(Slot {
                request: None,
                busy: false,
            }),
            stopping: AtomicBool::new(false),
            abandoned: AtomicBool::new(false),
            stop_generation: AtomicU32::new(0),
            scan_active: AtomicBool::new(false),
            status: AtomicU32::new(0),
            fault: AtomicU32::new(0),
        }
    }
    pub fn status(&self) -> ProviderStatus {
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
    pub fn fault(&self) -> Option<Error> {
        let stored = self.fault.load(Ordering::Acquire);
        (stored != 0).then(|| error_from_code(stored - 1))
    }
    pub fn stop_generation(&self) -> u32 {
        self.stop_generation.load(Ordering::Acquire)
    }
    pub fn stop_requested(&self) -> bool {
        self.stopping.load(Ordering::Acquire)
    }
    pub(super) fn mark_stopping_unless_terminal(&self) {
        loop {
            let old = self.status.load(Ordering::Acquire);
            if old == encode_status(ProviderStatus::Stopped)
                || matches!(decode_status(old), ProviderStatus::Failed(_))
            {
                return;
            }
            if self
                .status
                .compare_exchange_weak(
                    old,
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

#[derive(Clone, Copy)]
struct Peer<K> {
    key: K,
    id: PeerId,
    seen: bool,
}

/// Unexpected unwind must not release native callback storage that may still
/// be active. Normal teardown explicitly releases the guard after native close.
struct QuietGuard<G>(Option<G>);
impl<G> Drop for QuietGuard<G> {
    fn drop(&mut self) {
        if let Some(guard) = self.0.take() {
            core::mem::forget(guard);
        }
    }
}

fn run<N: Scanner, S: EventSink<Event>, G: Send + 'static>(
    mut native: N,
    mut sink: S,
    session: SessionId,
    state: std::sync::Arc<Shared>,
    mut guard: QuietGuard<G>,
) {
    let event = |kind| Event { session, kind };
    let mut peers: [Option<Peer<N::Peer>>; PEERS] = [None; PEERS];
    let mut active_filter = None;
    let mut scanning = false;
    let mut limited_reported = false;
    if let Err(error) = native.check() {
        fail(&mut sink, &state, &event, error);
    } else if let Err(error) = native.bind_session(session) {
        fail(&mut sink, &state, &event, error);
    } else if state.mark_ready()
        && !emit(
            &mut sink,
            event(EventKind::Status(ProviderStatus::Ready)),
            &state,
        )
        && !state.stop_requested()
    {
        fail(&mut sink, &state, &event, Error::Io);
    }

    let mut observed_stop = 0;
    let mut retry_delay = STOP_RETRY_INITIAL;
    loop {
        let generation = state.stop_generation();
        let stop_due = generation != observed_stop || state.abandoned.load(Ordering::Acquire);
        if stop_due {
            observed_stop = generation;
            if scanning {
                match native.stop_scan() {
                    Ok(()) => {
                        state.scan_active.store(false, Ordering::Release);
                    }
                    Err(error) => {
                        fail(&mut sink, &state, &event, error);
                        if state.abandoned.load(Ordering::Acquire) {
                            thread::park_timeout(retry_delay);
                            retry_delay = retry_delay.saturating_mul(2).min(STOP_RETRY_MAX);
                        } else {
                            // An explicit owner controls retries; park until its next request.
                            thread::park();
                        }
                        continue;
                    }
                }
            }
            native.close();
            break;
        }

        let request = match state.slot.lock() {
            Ok(mut slot) if !state.stop_requested() => slot.request.take(),
            _ => None,
        };
        if let Some(request) = request {
            match request.command {
                Command::StartScan { service } => {
                    match native
                        .configure_filter(service)
                        .and_then(|()| native.start_scan())
                    {
                        Ok(()) => {
                            scanning = true;
                            active_filter = service;
                            limited_reported = false;
                            state.scan_active.store(true, Ordering::Release);
                            for peer in peers.iter_mut().flatten() {
                                peer.seen = false;
                            }
                            state.slot.lock().unwrap().busy = false;
                            if !emit_start_completion(
                                &mut sink,
                                event(EventKind::Completed {
                                    operation: request.id.operation,
                                    result: Ok(Completion::ScanStarted),
                                }),
                                &state,
                            ) && !state.stop_requested()
                            {
                                fail(&mut sink, &state, &event, Error::Io);
                            }
                        }
                        Err(error) => {
                            state.slot.lock().unwrap().busy = false;
                            fail(&mut sink, &state, &event, error);
                        }
                    }
                }
                Command::StopScan => {
                    match native.stop_scan() {
                        Ok(()) => {
                            scanning = false;
                            active_filter = None;
                            state.scan_active.store(false, Ordering::Release);
                            state.slot.lock().unwrap().busy = false;
                            if !emit(
                                &mut sink,
                                event(EventKind::Completed {
                                    operation: request.id.operation,
                                    result: Ok(Completion::ScanStopped),
                                }),
                                &state,
                            ) && !state.stop_requested()
                            {
                                fail(&mut sink, &state, &event, Error::Io);
                            }
                        }
                        Err(error) => {
                            // Preserve active state; request_stop retries teardown.
                            state.slot.lock().unwrap().busy = false;
                            fail(&mut sink, &state, &event, error);
                            thread::park();
                        }
                    }
                }
            }
            continue;
        }
        if matches!(state.status(), ProviderStatus::Failed(_)) {
            thread::park();
            continue;
        }
        if scanning {
            let mut reports = [ScanReport::default(); BATCH];
            match native.get(active_filter, &mut reports) {
                Ok(count) if count <= reports.len() => {
                    for report in reports.iter().take(count) {
                        if state.stop_requested() || has_stop_request(&state) {
                            break;
                        }
                        if active_filter.is_some() && !report.matches_filter {
                            continue;
                        }
                        let found = peers
                            .iter()
                            .position(|entry| entry.is_some_and(|peer| peer.key == report.peer));
                        let index = match found {
                            Some(index) => index,
                            None => {
                                let Some(index) = peers.iter().position(Option::is_none) else {
                                    if !limited_reported {
                                        limited_reported = true;
                                        if !emit(
                                            &mut sink,
                                            event(EventKind::DiscoveryLimited {
                                                limit: PEERS as u16,
                                            }),
                                            &state,
                                        ) && !state.stop_requested()
                                            && !has_stop_request(&state)
                                        {
                                            fail(&mut sink, &state, &event, Error::Io);
                                        }
                                    }
                                    continue;
                                };
                                // Slots are never evicted, so this identity cannot
                                // alias another peer during the same session.
                                let id = PeerId(index as u64 + 1);
                                if let Err(error) = native.remember_peer(session, id, report.peer) {
                                    fail(&mut sink, &state, &event, error);
                                    break;
                                }
                                peers[index] = Some(Peer {
                                    key: report.peer,
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
                            event(EventKind::Discovered {
                                peer: peer.id,
                                rssi_dbm: report.rssi_dbm,
                            }),
                            &state,
                        ) {
                            if !state.stop_requested() && !has_stop_request(&state) {
                                fail(&mut sink, &state, &event, Error::Io);
                            }
                            break;
                        }
                    }
                }
                Ok(_) => fail(&mut sink, &state, &event, Error::Io),
                Err(error) => fail(&mut sink, &state, &event, error),
            }
            if !state.stop_requested() {
                thread::park_timeout(POLL);
            }
            continue;
        }
        thread::park();
    }

    state.scan_active.store(false, Ordering::Release);
    if let Ok(mut slot) = state.slot.lock() {
        slot.request = None;
        slot.busy = false;
    }
    drop(guard.0.take());
    state.set_status(ProviderStatus::Stopped);
    let _ = sink.try_emit(event(EventKind::Status(ProviderStatus::Stopped)));
}

fn fail<S: EventSink<Event>>(
    sink: &mut S,
    state: &Shared,
    event: &impl Fn(EventKind) -> Event,
    error: Error,
) {
    let _ = state
        .fault
        .compare_exchange(0, error as u32 + 1, Ordering::AcqRel, Ordering::Acquire);
    state.set_status(ProviderStatus::Failed(error));
    let _ = emit(
        sink,
        event(EventKind::Status(ProviderStatus::Failed(error))),
        state,
    );
}
fn has_stop_request(state: &Shared) -> bool {
    state
        .slot
        .lock()
        .ok()
        .and_then(|slot| slot.request)
        .is_some_and(|r| matches!(r.command, Command::StopScan))
}
fn emit<S: EventSink<Event>>(sink: &mut S, mut event: Event, state: &Shared) -> bool {
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
fn emit_start_completion<S: EventSink<Event>>(
    sink: &mut S,
    mut event: Event,
    state: &Shared,
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
        4 => Error::InvalidValue,
        5 => Error::Timeout,
        _ => Error::Io,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn callback_guard_is_not_released_by_unwind_before_quiescence() {
        struct Guard<'a>(&'a AtomicBool);
        impl Drop for Guard<'_> {
            fn drop(&mut self) {
                self.0.store(true, Ordering::Release);
            }
        }
        let released = AtomicBool::new(false);
        let result = std::panic::catch_unwind(|| {
            let _held = QuietGuard(Some(Guard(&released)));
            panic!("scripted pre-quiescence worker unwind");
        });
        assert!(result.is_err());
        assert!(!released.load(Ordering::Acquire));
        let mut quiet = QuietGuard(Some(Guard(&released)));
        drop(quiet.0.take());
        assert!(released.load(Ordering::Acquire));
    }

    #[test]
    fn stop_generation_exhaustion_does_not_wrap_or_stop_the_session() {
        let shared = Arc::new(Shared::new());
        shared.set_status(ProviderStatus::Ready);
        shared.stop_generation.store(u32::MAX, Ordering::Release);
        let mut device = Worker {
            session: SessionId::new(1).unwrap(),
            shared,
            worker: None,
            finish_error: None,
        };
        assert_eq!(device.request_stop(), Err(Error::Unavailable));
        assert_eq!(device.status(), ProviderStatus::Ready);
        assert!(!device.shared.stop_requested());
        assert_eq!(device.shared.stop_generation(), u32::MAX);
    }

    #[test]
    fn stopping_publication_cannot_overwrite_quiescent_or_failed_state() {
        let shared = Shared::new();
        for status in [ProviderStatus::Stopped, ProviderStatus::Failed(Error::Io)] {
            shared.set_status(status);
            shared.mark_stopping_unless_terminal();
            assert_eq!(shared.status(), status);
            assert!(!shared.mark_ready());
        }
    }
}
