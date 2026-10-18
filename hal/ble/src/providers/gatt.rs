//! Shared fixed-width GATT bridge; each OS owns callback storage until quiet.
#![allow(unsafe_code)]

use crate::gatt::{Command, Event, EventKind, Failure, Reply, Request, Value};
use crate::{gatt::Driver, Rejected};
use crate::{Error, OperationId, RequestId, SessionId};
use core::ffi::c_void;
use core::num::NonZeroU16;
use std::boxed::Box;
use std::io;

const CAPACITY: usize = 64;

#[repr(C)]
#[derive(Clone, Copy)]
struct RawCommand {
    operation: u64,
    peer_session: u64,
    peer_id: u64,
    kind: u32,
    handle: u16,
    length: u16,
    service: [u8; 16],
    characteristic: [u8; 16],
    data: [u8; CAPACITY],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct RawEvent {
    operation: u64,
    kind: u32,
    error: i32,
    handle: u16,
    length: u16,
    att_error: u8,
    reason: u8,
    data: [u8; CAPACITY],
}

unsafe extern "C" {
    fn nxrs_ble_gatt_open(
        session: u64,
        capacity: u16,
        wake: unsafe extern "C" fn(*mut c_void),
        release: unsafe extern "C" fn(*mut c_void),
        arg: *mut c_void,
    ) -> i32;
    fn nxrs_ble_gatt_submit(session: u64, command: *const RawCommand) -> i32;
    fn nxrs_ble_gatt_recover(session: u64, operation: u64);
    fn nxrs_ble_gatt_poll(session: u64, event: *mut RawEvent) -> i32;
    fn nxrs_ble_gatt_close(session: u64);
}

/// Desktop SDKs resolve a scan ID before C admission. NuttX already resolves it
/// in its critical-section registry. This zero-sized policy adds no MCU field.
pub(crate) trait PeerResolver {
    fn resolve(peer: crate::gatt::Peer) -> Result<Option<[u8; 16]>, Error>;
}

pub(crate) struct NativePeer;
impl PeerResolver for NativePeer {
    fn resolve(_: crate::gatt::Peer) -> Result<Option<[u8; 16]>, Error> {
        Ok(None)
    }
}

pub(crate) struct Native<const N: usize, P: PeerResolver = NativePeer> {
    session: SessionId,
    last_operation: Option<RequestId>,
    peer: core::marker::PhantomData<fn() -> P>,
}

#[cfg(any(nxrs_provider = "nuttx", test))]
pub(crate) fn open<const N: usize, W: Fn() + Send + Sync + 'static>(
    wake: W,
) -> Result<Native<N>, Error> {
    open_with::<N, NativePeer, W>(wake)
}

pub(crate) fn open_with<const N: usize, P: PeerResolver, W: Fn() + Send + Sync + 'static>(
    wake: W,
) -> Result<Native<N, P>, Error> {
    if N > CAPACITY {
        return Err(Error::Unsupported);
    }
    let session = crate::worker::new_session()?;
    // One stable wake context, not a thread or per-operation allocation.
    let context = Box::into_raw(Box::new(wake)).cast::<c_void>();
    unsafe extern "C" fn notify<W: Fn()>(context: *mut c_void) {
        // SAFETY: C holds the Box until its post-callback close barrier.
        unsafe { (&*context.cast::<W>())() };
    }
    unsafe extern "C" fn release<W>(context: *mut c_void) {
        // SAFETY: called exactly once after callback unregistration/drain.
        unsafe { drop(Box::from_raw(context.cast::<W>())) };
    }
    // SAFETY: C takes context ownership only on success. Session is unique.
    let status =
        unsafe { nxrs_ble_gatt_open(session.get(), N as u16, notify::<W>, release::<W>, context) };
    if status != 0 {
        // SAFETY: failed admission did not retain or invoke this context.
        unsafe { release::<W>(context) };
        return Err(device_error(status));
    }
    Ok(Native {
        session,
        last_operation: None,
        peer: core::marker::PhantomData,
    })
}

fn device_error(errno: i32) -> Error {
    match io::Error::from_raw_os_error(errno).kind() {
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted | io::ErrorKind::ResourceBusy => {
            Error::Busy
        }
        io::ErrorKind::NotFound | io::ErrorKind::NetworkDown | io::ErrorKind::NotConnected => {
            Error::Unavailable
        }
        io::ErrorKind::PermissionDenied => Error::PermissionDenied,
        io::ErrorKind::TimedOut => Error::Timeout,
        io::ErrorKind::InvalidInput
        | io::ErrorKind::InvalidData
        | io::ErrorKind::AlreadyExists
        | io::ErrorKind::FileTooLarge => Error::InvalidValue,
        io::ErrorKind::Unsupported => Error::Unsupported,
        _ => Error::Io,
    }
}

fn command<const N: usize>(request: Request<N>) -> Result<RawCommand, Error> {
    let mut raw = RawCommand {
        operation: request.id.operation.get(),
        peer_session: 0,
        peer_id: 0,
        kind: 0,
        handle: 0,
        length: 0,
        service: [0; 16],
        characteristic: [0; 16],
        data: [0; CAPACITY],
    };
    match request.command {
        Command::Connect { peer } => {
            raw.kind = 1;
            raw.peer_session = peer.session.get();
            raw.peer_id = peer.id.0;
        }
        Command::Discover {
            service,
            characteristic,
        } => {
            raw.kind = 2;
            raw.service = service.0;
            raw.characteristic = characteristic.0;
        }
        Command::Read { attribute } => {
            raw.kind = 3;
            raw.handle = attribute.handle().get();
        }
        Command::Write { attribute, value } => {
            let bytes = value.as_slice();
            if bytes.len() > CAPACITY {
                return Err(Error::InvalidValue);
            }
            raw.kind = 4;
            raw.handle = attribute.handle().get();
            raw.length = bytes.len() as u16;
            raw.data[..bytes.len()].copy_from_slice(bytes);
        }
        Command::Disconnect => raw.kind = 5,
    }
    Ok(raw)
}

fn decode<const N: usize>(session: SessionId, fallback: RequestId, raw: RawEvent) -> Event<N> {
    let id = OperationId::new(raw.operation).map(|operation| RequestId { session, operation });
    let protocol_error = || EventKind::Completed {
        request: fallback,
        result: Err(Failure::Protocol),
    };
    let kind = match (id, raw.kind) {
        (Some(id), 6) => EventKind::Disconnected {
            connection: id,
            reason: Some(raw.reason),
        },
        (Some(id), 8) => EventKind::Disconnected {
            connection: id,
            reason: None,
        },
        (Some(id), 7) => EventKind::Quiescent { recovery: id },
        (Some(id), 9) if raw.error > 0 => EventKind::Completed {
            request: id,
            result: Err(Failure::Remote(device_error(raw.error))),
        },
        (Some(id), 1..=5) => {
            let result = if raw.error != 0 {
                let failure = match io::Error::from_raw_os_error(raw.error).kind() {
                    io::ErrorKind::NotConnected
                    | io::ErrorKind::ConnectionAborted
                    | io::ErrorKind::ConnectionReset => Failure::Disconnected,
                    _ => Failure::Device(device_error(raw.error)),
                };
                Err(failure)
            } else if raw.att_error != 0 {
                Err(Failure::Att(raw.att_error))
            } else {
                match raw.kind {
                    1 => Ok(Reply::Connected),
                    2 => NonZeroU16::new(raw.handle)
                        .map(|handle| Reply::Discovered { handle })
                        .ok_or(Failure::Protocol),
                    3 if usize::from(raw.length) <= CAPACITY => {
                        Value::new(&raw.data[..usize::from(raw.length)])
                            .map(Reply::Read)
                            .ok_or(Failure::Protocol)
                    }
                    4 => Ok(Reply::Written),
                    5 => Ok(Reply::Disconnected),
                    _ => Err(Failure::Protocol),
                }
            };
            EventKind::Completed {
                request: id,
                result,
            }
        }
        _ => protocol_error(),
    };
    Event { session, kind }
}

impl<const N: usize, P: PeerResolver> Driver<N> for Native<N, P> {
    fn session(&self) -> SessionId {
        self.session
    }

    fn submit(&mut self, request: Request<N>) -> Result<(), Rejected<Request<N>, Error>> {
        if request.id.session != self.session {
            return Err(Rejected {
                value: request,
                error: Error::InvalidValue,
            });
        }
        let mut raw = command(request).map_err(|error| Rejected {
            value: request,
            error,
        })?;
        if let Command::Connect { peer } = request.command {
            if let Some(key) = P::resolve(peer).map_err(|error| Rejected {
                value: request,
                error,
            })? {
                // The UUID fields are otherwise unused by Connect. C copies
                // this identity at admission; closing the scanner cannot revoke it.
                raw.service = key;
            }
        }
        // SAFETY: C copies the fixed-size command synchronously; no borrowed pointers survive.
        let status = unsafe { nxrs_ble_gatt_submit(self.session.get(), &raw) };
        if status == 0 {
            self.last_operation = Some(request.id);
            Ok(())
        } else {
            Err(Rejected {
                value: request,
                error: device_error(status),
            })
        }
    }

    fn recover(&mut self, token: RequestId) {
        if token.session == self.session {
            // SAFETY: nonblocking priority flag in session-owned static storage.
            unsafe { nxrs_ble_gatt_recover(self.session.get(), token.operation.get()) };
        }
    }

    fn poll(&mut self) -> Option<Event<N>> {
        let mut raw = RawEvent {
            operation: 0,
            kind: 0,
            error: 0,
            handle: 0,
            length: 0,
            att_error: 0,
            reason: 0,
            data: [0; CAPACITY],
        };
        // SAFETY: C copies one retained event into writable ABI storage.
        if unsafe { nxrs_ble_gatt_poll(self.session.get(), &mut raw) } == 0 {
            return None;
        }
        let fallback = self.last_operation.unwrap_or(RequestId {
            session: self.session,
            operation: OperationId::new(1).unwrap(),
        });
        Some(decode(self.session, fallback, raw))
    }
}

impl<const N: usize, P: PeerResolver> Drop for Native<N, P> {
    fn drop(&mut self) {
        // SAFETY: records close; C retains the wake context and lease until quiet.
        unsafe { nxrs_ble_gatt_close(self.session.get()) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::ffi::c_void;
    use std::collections::VecDeque;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
    use std::vec;
    use std::vec::Vec;

    type Callback = unsafe extern "C" fn(*mut c_void);

    #[derive(Default)]
    struct FakeFfiState {
        open_status: i32,
        open_calls: usize,
        close_calls: usize,
        submit_calls: usize,
        recover_calls: Vec<(u64, u64)>,
        poll_calls: usize,
        accepted_command: Option<RawCommand>,
        events: VecDeque<RawEvent>,
        wake: Option<Callback>,
        release: Option<Callback>,
        context: usize,
    }

    static FFI_STATE: OnceLock<Mutex<FakeFfiState>> = OnceLock::new();
    static SERIAL: Mutex<()> = Mutex::new(());

    fn state() -> &'static Mutex<FakeFfiState> {
        FFI_STATE.get_or_init(|| Mutex::new(FakeFfiState::default()))
    }

    fn serial() -> MutexGuard<'static, ()> {
        SERIAL
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn reset_fake(status: i32) {
        *state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = FakeFfiState {
            open_status: status,
            ..FakeFfiState::default()
        };
    }

    fn fake_snapshot() -> FakeFfiState {
        let state = state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        FakeFfiState {
            open_status: state.open_status,
            open_calls: state.open_calls,
            close_calls: state.close_calls,
            submit_calls: state.submit_calls,
            recover_calls: state.recover_calls.clone(),
            poll_calls: state.poll_calls,
            accepted_command: state.accepted_command,
            events: state.events.clone(),
            wake: state.wake,
            release: state.release,
            context: state.context,
        }
    }

    fn invoke_wake() {
        let (wake, context) = {
            let state = state()
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            (state.wake, state.context)
        };
        if let Some(wake) = wake {
            // SAFETY: the fake C open captured this live callback/context pair.
            unsafe { wake(context as *mut c_void) };
        }
    }

    fn invoke_release() {
        let (release, context) = {
            let mut state = state()
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            (state.release.take(), state.context)
        };
        if let Some(release) = release {
            // SAFETY: the fake C open captured ownership and invokes release once.
            unsafe { release(context as *mut c_void) };
        }
    }

    #[unsafe(no_mangle)]
    unsafe extern "C" fn nxrs_ble_gatt_open(
        _session: u64,
        _capacity: u16,
        wake: Callback,
        release: Callback,
        arg: *mut c_void,
    ) -> i32 {
        let mut state = state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.open_calls += 1;
        if state.open_status == 0 {
            state.wake = Some(wake);
            state.release = Some(release);
            state.context = arg as usize;
        }
        state.open_status
    }

    #[unsafe(no_mangle)]
    unsafe extern "C" fn nxrs_ble_gatt_submit(_session: u64, command: *const RawCommand) -> i32 {
        let mut state = state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.submit_calls += 1;
        // SAFETY: Native::submit passes a pointer to a live fixed-size command.
        state.accepted_command = Some(unsafe { *command });
        0
    }

    #[unsafe(no_mangle)]
    unsafe extern "C" fn nxrs_ble_gatt_recover(session: u64, operation: u64) {
        state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .recover_calls
            .push((session, operation));
    }

    #[unsafe(no_mangle)]
    unsafe extern "C" fn nxrs_ble_gatt_poll(_session: u64, event: *mut RawEvent) -> i32 {
        let mut state = state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.poll_calls += 1;
        if let Some(next) = state.events.pop_front() {
            // SAFETY: Native::poll passes writable storage for one RawEvent.
            unsafe { *event = next };
            1
        } else {
            0
        }
    }

    #[unsafe(no_mangle)]
    unsafe extern "C" fn nxrs_ble_gatt_close(_session: u64) {
        state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .close_calls += 1;
    }

    struct CapturedDropCounter(Arc<AtomicUsize>);

    impl Drop for CapturedDropCounter {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn counted_wake(drops: &Arc<AtomicUsize>, wakes: &Arc<AtomicUsize>) -> impl Fn() + Send + Sync {
        let captured = CapturedDropCounter(Arc::clone(drops));
        let wakes = Arc::clone(wakes);
        move || {
            let _keep_capture_alive = &captured;
            wakes.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn id() -> RequestId {
        RequestId {
            session: SessionId::new(4).unwrap(),
            operation: OperationId::new(3).unwrap(),
        }
    }

    #[test]
    fn rejected_open_drops_the_wake_capture_once() {
        let _serial = serial();
        // EINVAL and EBUSY on both supported Unix test hosts and NuttX.
        for (status, expected) in [(22, Error::InvalidValue), (16, Error::Busy)] {
            reset_fake(status);
            let drops = Arc::new(AtomicUsize::new(0));
            let wakes = Arc::new(AtomicUsize::new(0));

            let result = open::<20, _>(counted_wake(&drops, &wakes));

            assert_eq!(result.err(), Some(expected));
            assert_eq!(drops.load(Ordering::SeqCst), 1);
            assert_eq!(wakes.load(Ordering::SeqCst), 0);
            let state = fake_snapshot();
            assert_eq!(state.open_calls, 1);
            assert_eq!(state.close_calls, 0);
            assert!(state.context == 0);
        }
    }

    #[test]
    fn close_keeps_wake_context_alive_until_post_callback_release() {
        let _serial = serial();
        reset_fake(0);
        let drops = Arc::new(AtomicUsize::new(0));
        let wakes = Arc::new(AtomicUsize::new(0));
        let native = open::<20, _>(counted_wake(&drops, &wakes)).unwrap();
        let admitted = fake_snapshot();
        assert!(admitted.context != 0);

        invoke_wake();
        assert_eq!(wakes.load(Ordering::SeqCst), 1);
        drop(native);
        assert_eq!(fake_snapshot().close_calls, 1);
        assert_eq!(drops.load(Ordering::SeqCst), 0);

        invoke_wake();
        assert_eq!(wakes.load(Ordering::SeqCst), 2);
        assert_eq!(drops.load(Ordering::SeqCst), 0);
        invoke_release();
        assert_eq!(drops.load(Ordering::SeqCst), 1);
        assert!(fake_snapshot().release.is_none());
    }

    #[test]
    fn native_driver_submits_recovers_and_polls_through_the_c_bridge() {
        let _serial = serial();
        reset_fake(0);
        let mut native = open::<20, _>(|| {}).unwrap();
        let request_id = RequestId {
            session: native.session(),
            operation: OperationId::new(9).unwrap(),
        };
        let request = Request {
            id: request_id,
            link: None,
            command: Command::Connect {
                peer: crate::gatt::Peer {
                    session: SessionId::new(7).unwrap(),
                    id: crate::PeerId(11),
                },
            },
        };

        native.submit(request).unwrap();
        native.recover(request_id);
        let submitted = fake_snapshot();
        let command = submitted.accepted_command.unwrap();
        assert_eq!(
            (
                command.operation,
                command.peer_session,
                command.peer_id,
                command.kind
            ),
            (9, 7, 11, 1)
        );
        assert_eq!(submitted.submit_calls, 1);
        assert_eq!(submitted.recover_calls, vec![(native.session().get(), 9)]);

        state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .events
            .push_back(RawEvent {
                operation: 9,
                kind: 1,
                error: 0,
                handle: 0,
                length: 0,
                att_error: 0,
                reason: 0,
                data: [0; CAPACITY],
            });
        assert!(matches!(
            native.poll().unwrap().kind,
            EventKind::Completed {
                request,
                result: Ok(Reply::Connected),
            } if request == request_id
        ));
        drop(native);
        assert_eq!(fake_snapshot().close_calls, 1);
        invoke_release();
    }

    #[test]
    fn desktop_resolution_copies_identity_or_rejects_before_foreign_admission() {
        struct Identity;
        impl PeerResolver for Identity {
            fn resolve(peer: crate::gatt::Peer) -> Result<Option<[u8; 16]>, Error> {
                Ok(Some([peer.id.0 as u8; 16]))
            }
        }
        struct Missing;
        impl PeerResolver for Missing {
            fn resolve(_: crate::gatt::Peer) -> Result<Option<[u8; 16]>, Error> {
                Err(Error::Unavailable)
            }
        }
        assert_eq!(
            core::mem::size_of::<Native<20, Identity>>(),
            core::mem::size_of::<Native<20>>()
        );
        let _serial = serial();
        reset_fake(0);
        let mut native = open_with::<20, Identity, _>(|| {}).unwrap();
        let request = Request {
            id: RequestId {
                session: native.session(),
                operation: OperationId::new(1).unwrap(),
            },
            link: None,
            command: Command::Connect {
                peer: crate::gatt::Peer {
                    session: id().session,
                    id: crate::PeerId(5),
                },
            },
        };
        native.submit(request).unwrap();
        assert_eq!(fake_snapshot().accepted_command.unwrap().service, [5; 16]);
        drop(native);
        invoke_release();
        reset_fake(0);
        let mut native = open_with::<20, Missing, _>(|| {}).unwrap();
        let request = Request {
            id: RequestId {
                session: native.session(),
                ..request.id
            },
            ..request
        };
        assert_eq!(
            native.submit(request),
            Err(Rejected {
                value: request,
                error: Error::Unavailable
            })
        );
        assert_eq!(fake_snapshot().submit_calls, 0);
        drop(native);
        invoke_release();
    }

    fn raw(kind: u32) -> RawEvent {
        RawEvent {
            operation: 3,
            kind,
            error: 0,
            handle: 0,
            length: 0,
            att_error: 0,
            reason: 0,
            data: [0; CAPACITY],
        }
    }
    #[test]
    fn bridge_layout_and_connect_identity_are_fixed_width() {
        assert_eq!(core::mem::size_of::<RawCommand>(), 128);
        assert_eq!(core::mem::size_of::<RawEvent>(), 88);
        let request = Request::<20> {
            id: id(),
            link: None,
            command: Command::Connect {
                peer: crate::gatt::Peer {
                    session: SessionId::new(2).unwrap(),
                    id: crate::PeerId(5),
                },
            },
        };
        let command = command(request).unwrap();
        assert_eq!(
            (
                command.operation,
                command.peer_session,
                command.peer_id,
                command.kind
            ),
            (3, 2, 5, 1)
        );
    }
    #[test]
    fn malformed_or_oversized_results_are_not_truncated_or_barriers() {
        for value in [
            raw(0),
            raw(99),
            raw(9), // remote errors require a nonzero normalized error
            RawEvent {
                operation: 0,
                ..raw(7)
            },
            raw(2),
            RawEvent {
                length: 21,
                ..raw(3)
            },
            RawEvent {
                length: 65,
                ..raw(3)
            },
        ] {
            assert!(matches!(
                decode::<20>(id().session, id(), value).kind,
                EventKind::Completed {
                    result: Err(Failure::Protocol),
                    ..
                }
            ));
        }
    }
    #[test]
    fn att_error_and_disconnect_reason_have_separate_meanings() {
        assert!(matches!(
            decode::<20>(
                id().session,
                id(),
                RawEvent {
                    error: 13,
                    ..raw(9)
                }
            )
            .kind,
            EventKind::Completed {
                result: Err(Failure::Remote(Error::PermissionDenied)),
                ..
            }
        ));
        assert!(matches!(
            decode::<20>(
                id().session,
                id(),
                RawEvent {
                    att_error: 8,
                    ..raw(3)
                }
            )
            .kind,
            EventKind::Completed {
                result: Err(Failure::Att(8)),
                ..
            }
        ));
        assert!(matches!(
            decode::<20>(
                id().session,
                id(),
                RawEvent {
                    reason: 8,
                    ..raw(6)
                }
            )
            .kind,
            EventKind::Disconnected {
                reason: Some(8),
                ..
            }
        ));
        assert!(matches!(decode::<20>(id().session, id(), raw(7)).kind,
            EventKind::Quiescent { recovery } if recovery == id()));
        assert!(matches!(
            decode::<20>(id().session, id(), raw(8)).kind,
            EventKind::Disconnected { reason: None, .. }
        ));
    }
}
