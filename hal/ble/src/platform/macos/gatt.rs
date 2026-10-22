//! CoreBluetooth GATT behavior. All SDK operations execute on its serial queue.
//! The front door uses try_lock; retirement signals never need that lock.
use crate::gatt_backend::{
    Backend, Event as BackendEvent, Kind, Outcome, Request as Operation, Wake, CAPACITY,
};
use crate::{Error, OperationId, SessionId};
use std::prelude::v1::*;
use std::{
    collections::VecDeque,
    ffi::c_void,
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex,
    },
};
unsafe extern "C" {
    fn mac_gatt_initialize();
    fn mac_gatt_allocate();
    fn mac_gatt_poke();
    fn mac_gatt_defer();
    fn mac_gatt_errno(kind: u32) -> i32;
    fn mac_gatt_authorization() -> i32;
    fn mac_gatt_manager();
    fn mac_gatt_manager_state() -> u32;
    fn mac_gatt_object(field: u32, index: usize) -> *mut c_void;
    fn mac_gatt_count(field: u32) -> usize;
    fn mac_gatt_identity(object: *mut c_void, bytes: *mut u8);
    fn mac_gatt_uuid(
        object: *mut c_void,
        characteristic: bool,
        bytes: *mut *const u8,
        len: *mut usize,
    );
    fn mac_gatt_retrieve(identity: *const u8, index: usize, count: *mut usize) -> *mut c_void;
    fn mac_gatt_scan();
    fn mac_gatt_stop_scan();
    fn mac_gatt_connect(peripheral: *mut c_void);
    fn mac_gatt_connected() -> bool;
    fn mac_gatt_cancel();
    fn mac_gatt_services(uuid: *const u8);
    fn mac_gatt_characteristics(service: *mut c_void, uuid: *const u8);
    fn mac_gatt_pin(attribute: *mut c_void);
    fn mac_gatt_properties(attribute: *mut c_void) -> u32;
    fn mac_gatt_max_write() -> usize;
    fn mac_gatt_read(attribute: *mut c_void);
    fn mac_gatt_write(attribute: *mut c_void, data: *const u8, len: usize);
    fn mac_gatt_detach(release: bool);
}
#[derive(Clone, Copy, PartialEq)]
enum Phase {
    Idle,
    Connecting,
    Services,
    Characteristics,
    Reading,
    Writing,
    Disconnecting,
    Retiring,
}
struct State {
    session: u64,
    capacity: usize,
    wake: Option<Wake>,
    admitted: Option<Operation>,
    command: Operation,
    result: Option<BackendEvent>,
    outcome: Outcome,
    events: VecDeque<BackendEvent>,
    busy: bool,
    closing: bool,
    poisoned: bool,
    fault: bool,
    phase: Phase,
    connection: u64,
    handled: u64,
    recovery: u64,
    issued: bool,
    quiet: bool,
    lost: bool,
    flush: bool,
    attributes: Vec<usize>,
}
static STATE: Mutex<Option<State>> = Mutex::new(None);
static CLOSE: AtomicU64 = AtomicU64::new(0);
static RECOVER_SESSION: AtomicU64 = AtomicU64::new(0);
static RECOVER_OP: AtomicU64 = AtomicU64::new(0);
static RETRY: AtomicU64 = AtomicU64::new(0);
fn err(kind: u32) -> i32 {
    unsafe { mac_gatt_errno(kind) }
}
fn poke() {
    unsafe { mac_gatt_poke() }
}
fn notify(value: Option<Wake>) {
    if let Some(wake) = value {
        wake.notify();
    }
}
impl State {
    fn wake(&self) -> Option<Wake> {
        self.wake.clone()
    }
    fn schedule(&mut self) {
        if !self.flush {
            self.flush = true;
            unsafe { mac_gatt_defer() }
        }
    }
    fn complete(&mut self, error: i32, att: u8) {
        if self.result.is_some() || matches!(self.phase, Phase::Idle | Phase::Retiring) {
            return;
        }
        self.outcome.native_error(error, att, err(9));
        self.result = Some(self.outcome.finish(self.command));
        self.phase = Phase::Idle;
        self.schedule();
    }
    fn retire(&mut self) {
        unsafe { mac_gatt_stop_scan() };
        if self.issued && !self.quiet {
            unsafe { mac_gatt_cancel() }
        } else {
            self.quiet = true;
            self.schedule();
        }
    }
    fn identity(&self, object: *mut c_void) -> bool {
        let mut bytes = [0; 16];
        unsafe { mac_gatt_identity(object, bytes.as_mut_ptr()) };
        self.command.identity() == Some(bytes)
    }
    fn connect(&mut self) {
        if self.phase != Phase::Connecting || self.issued {
            return;
        }
        let Some(identity) = self.command.identity() else {
            self.complete(err(1), 0);
            return;
        };
        let mut count = 0;
        let first = unsafe { mac_gatt_retrieve(identity.as_ptr(), 0, &mut count) };
        let mut found = None;
        for i in 0..count {
            let p = if i == 0 {
                first
            } else {
                unsafe { mac_gatt_retrieve(identity.as_ptr(), i, &mut 0) }
            };
            if self.identity(p) {
                if found.is_some() {
                    self.complete(err(6), 0);
                    return;
                }
                found = Some(p);
            }
        }
        if let Some(p) = found {
            self.issued = true;
            self.quiet = false;
            unsafe { mac_gatt_connect(p) }
        } else {
            unsafe { mac_gatt_scan() }
        }
    }
    fn start(&mut self) {
        if self.command.kind() == Kind::Connect {
            self.phase = Phase::Connecting;
            self.quiet = true;
            let e = unsafe { mac_gatt_authorization() };
            if e != 0 {
                self.complete(e, 0);
                return;
            }
            unsafe { mac_gatt_manager() };
            if unsafe { mac_gatt_manager_state() } == 0 {
                self.connect();
            }
            return;
        }
        if !unsafe { mac_gatt_connected() } {
            self.phase = Phase::Reading;
            self.complete(err(4), 0);
            return;
        }
        match self.command.kind() {
            Kind::Disconnect => {
                self.phase = Phase::Disconnecting;
                self.retire();
            }
            Kind::Discover => {
                self.phase = Phase::Services;
                unsafe { mac_gatt_services(self.command.service().as_ptr()) }
            }
            _ => {
                self.phase = if self.command.kind() == Kind::Read {
                    Phase::Reading
                } else {
                    Phase::Writing
                };
                let Some(index) = usize::from(self.command.handle()).checked_sub(1) else {
                    self.complete(err(1), 0);
                    return;
                };
                let Some(&attribute) = self.attributes.get(index) else {
                    self.complete(err(1), 0);
                    return;
                };
                let attribute = attribute as *mut c_void;
                let properties = unsafe { mac_gatt_properties(attribute) };
                if properties & 1 != 0
                    || properties
                        & if self.command.kind() == Kind::Read {
                            2
                        } else {
                            4
                        }
                        == 0
                {
                    self.complete(err(9), 0);
                    return;
                }
                if self.command.kind() == Kind::Read {
                    unsafe { mac_gatt_read(attribute) }
                } else {
                    if self.command.value().len() > unsafe { mac_gatt_max_write() } {
                        self.complete(err(9), 0);
                        return;
                    }
                    unsafe {
                        mac_gatt_write(
                            attribute,
                            self.command.value().as_ptr(),
                            self.command.value().len(),
                        )
                    }
                }
            }
        }
    }
    fn matched(&self, object: *mut c_void, characteristic: bool, target: [u8; 16]) -> bool {
        let mut data = std::ptr::null();
        let mut len = 0;
        unsafe { mac_gatt_uuid(object, characteristic, &mut data, &mut len) };
        len != 0
            && super::uuid::canonical(unsafe { std::slice::from_raw_parts(data, len) })
                == Some(target)
    }
    fn discovery(&mut self, characteristic: bool) {
        let field = if characteristic { 6 } else { 5 };
        let target = if characteristic {
            self.command.characteristic()
        } else {
            self.command.service()
        };
        let mut found = None;
        for i in 0..unsafe { mac_gatt_count(field) } {
            let object = unsafe { mac_gatt_object(field, i) };
            if self.matched(object, characteristic, target) {
                if found.is_some() {
                    self.complete(err(6), 0);
                    return;
                }
                found = Some(object);
            }
        }
        let Some(object) = found else {
            self.complete(err(7), 0);
            return;
        };
        if !characteristic {
            self.phase = Phase::Characteristics;
            unsafe { mac_gatt_characteristics(object, self.command.characteristic().as_ptr()) };
            return;
        }
        let index = if let Some(i) = self.attributes.iter().position(|&p| p == object as usize) {
            i
        } else {
            if self.attributes.len() == 32 {
                self.complete(err(8), 0);
                return;
            }
            let i = self.attributes.len();
            self.attributes.push(object as usize);
            unsafe { mac_gatt_pin(object) };
            i
        };
        self.outcome.handle = (index + 1) as u16;
        self.complete(0, 0);
    }
}
#[no_mangle]
unsafe extern "C" fn mac_gatt_control_rust() {
    let mut guard = STATE.lock().unwrap();
    let Some(s) = guard.as_mut() else {
        return;
    };
    if CLOSE.load(Ordering::Acquire) == s.session {
        s.closing = true;
    }
    let token = if RECOVER_SESSION.load(Ordering::Acquire) == s.session {
        RECOVER_OP.load(Ordering::Acquire)
    } else {
        0
    };
    let recover = token != 0
        && token != s.handled
        && (token == s.command.operation
            || token == s.connection
            || s.admitted.is_some_and(|c| c.operation == token));
    if s.closing || recover {
        s.admitted = None;
        s.busy = true;
        if recover {
            s.recovery = token;
            s.handled = token;
        }
        s.phase = Phase::Retiring;
        s.result = None;
        s.retire();
        return;
    }
    let waiting = !s.events.is_empty() || s.fault || RETRY.swap(0, Ordering::AcqRel) == s.session;
    let wake = if waiting { s.wake() } else { None };
    if let Some(c) = s.admitted.take() {
        s.command = c;
        s.outcome = Outcome::EMPTY;
        s.start();
    } else if s.result.is_some()
        || s.lost
        || matches!(s.phase, Phase::Retiring | Phase::Disconnecting)
    {
        s.schedule();
    }
    drop(guard);
    notify(wake);
}
#[no_mangle]
unsafe extern "C" fn mac_gatt_flush_rust() {
    {
        let mut guard = STATE.lock().unwrap();
        let Some(s) = guard.as_mut() else {
            return;
        };
        s.flush = false;
    }
    loop {
        let mut guard = STATE.lock().unwrap();
        let Some(s) = guard.as_mut() else {
            return;
        };
        if s.closing {
            s.result = None;
            s.lost = false;
        }
        let mut terminal = false;
        let event = if let Some(result) = s.result {
            Some((result, s.recovery == 0 && !s.closing, 1))
        } else if s.lost {
            let e = BackendEvent::Disconnected {
                connection: s.connection,
                reason: None,
            };
            Some((e, false, 2))
        } else if s.quiet && matches!(s.phase, Phase::Retiring | Phase::Disconnecting) {
            unsafe { mac_gatt_detach(s.closing) };
            s.issued = false;
            s.attributes.clear();
            if s.closing {
                let wake = s.wake.take();
                *guard = None;
                drop(guard);
                drop(wake);
                return;
            }
            let e = if s.phase == Phase::Disconnecting {
                BackendEvent::Completed {
                    operation: s.command.operation,
                    result: Ok(crate::gatt::Reply::Disconnected),
                }
            } else {
                BackendEvent::Quiescent {
                    recovery: s.recovery,
                }
            };
            terminal = true;
            Some((e, true, 3))
        } else {
            None
        };
        let Some((event, release_busy, source)) = event else {
            return;
        };
        let wake = s.wake();
        if s.events.len() == 4 {
            s.poisoned = true;
            s.fault = true;
            drop(guard);
            notify(wake);
            return;
        }
        s.events.push_back(event);
        if release_busy {
            s.busy = false;
        }
        match source {
            1 => s.result = None,
            2 => s.lost = false,
            _ => {
                s.recovery = 0;
                s.connection = 0;
                s.phase = Phase::Idle;
            }
        }
        drop(guard);
        notify(wake);
        if terminal {
            return;
        }
    }
}
#[no_mangle]
unsafe extern "C" fn mac_gatt_event_rust(
    kind: u32,
    manager: *mut c_void,
    peripheral: *mut c_void,
    object: *mut c_void,
    error: i32,
    att: u8,
    data: *const u8,
    len: usize,
) {
    let mut guard = STATE.lock().unwrap();
    let Some(s) = guard.as_mut() else {
        return;
    };
    if kind <= 5 && manager != unsafe { mac_gatt_object(1, 0) } {
        return;
    }
    if kind >= 3 && peripheral != unsafe { mac_gatt_object(2, 0) } {
        return;
    }
    match kind {
        1 => {
            let status = unsafe { mac_gatt_manager_state() };
            if status == 0 {
                s.connect();
                return;
            }
            if status == 1 && s.phase == Phase::Connecting && !s.issued {
                return;
            }
            s.quiet = true;
            if s.connection != 0 {
                s.lost = true;
            }
            s.complete(
                err(match status {
                    2 => 10,
                    3 => 9,
                    _ => 11,
                }),
                0,
            );
            s.schedule();
        }
        2 => {
            if s.phase != Phase::Connecting || s.issued || !s.identity(peripheral) {
                return;
            }
            unsafe { mac_gatt_stop_scan() };
            s.issued = true;
            s.quiet = false;
            unsafe { mac_gatt_connect(peripheral) }
        }
        3 => {
            if matches!(s.phase, Phase::Retiring | Phase::Disconnecting) {
                unsafe { mac_gatt_cancel() };
                return;
            }
            if s.phase == Phase::Connecting {
                s.connection = s.command.operation;
                s.complete(0, 0);
            }
        }
        4 => {
            if !s.issued
                || !matches!(
                    s.phase,
                    Phase::Connecting | Phase::Retiring | Phase::Disconnecting
                )
            {
                return;
            }
            s.quiet = true;
            s.complete(
                if error == 0 && att == 0 {
                    err(4)
                } else {
                    error
                },
                att,
            );
            s.schedule();
        }
        5 => {
            if s.quiet {
                return;
            }
            s.quiet = true;
            if s.connection != 0 {
                s.lost = true;
            }
            if s.phase != Phase::Disconnecting {
                s.complete(err(4), 0);
            }
            s.schedule();
        }
        6 | 7 => {
            if (kind == 6 && s.phase != Phase::Services)
                || (kind == 7
                    && (s.phase != Phase::Characteristics
                        || object != unsafe { mac_gatt_object(3, 0) }))
            {
                return;
            }
            if error != 0 || att != 0 {
                s.complete(error, att);
            } else {
                s.discovery(kind == 7);
            }
        }
        8 | 9 => {
            if object != unsafe { mac_gatt_object(4, 0) }
                || s.phase
                    != if kind == 8 {
                        Phase::Reading
                    } else {
                        Phase::Writing
                    }
            {
                return;
            }
            if error != 0 || att != 0 {
                s.complete(error, att);
                return;
            }
            if kind == 8 {
                if len > s.capacity {
                    s.complete(err(if len == usize::MAX { 3 } else { 1 }), 0);
                    return;
                }
                s.outcome.length = len;
                if len != 0 {
                    unsafe { std::ptr::copy_nonoverlapping(data, s.outcome.data.as_mut_ptr(), len) }
                }
                s.complete(0, 0);
            } else {
                s.complete(0, 0);
            }
        }
        _ => {}
    }
}

pub(crate) struct Executor;
impl Backend for Executor {
    fn open(session: SessionId, capacity: usize, wake: Wake) -> Result<(), Error> {
        let session = session.get();

        if capacity > CAPACITY {
            return Err(Error::InvalidValue);
        }
        unsafe { mac_gatt_initialize() };
        let Ok(mut state) = STATE.try_lock() else {
            return Err(Error::Busy);
        };
        if state.is_some() {
            return Err(Error::Busy);
        }
        unsafe { mac_gatt_allocate() };
        *state = Some(State {
            session,
            capacity,
            wake: Some(wake),
            admitted: None,
            command: Operation::IDLE,
            result: None,
            outcome: Outcome::EMPTY,
            events: VecDeque::with_capacity(4),
            busy: false,
            closing: false,
            poisoned: false,
            fault: false,
            phase: Phase::Idle,
            connection: 0,
            handled: 0,
            recovery: 0,
            issued: false,
            quiet: true,
            lost: false,
            flush: false,
            attributes: Vec::with_capacity(32),
        });
        Ok(())
    }
    fn submit(session: SessionId, request: Operation) -> Result<(), Error> {
        let session = session.get();

        let c = request;
        if c.operation == 0 || c.kind() == Kind::Idle || c.value().len() > CAPACITY {
            return Err(Error::InvalidValue);
        }
        let Ok(mut guard) = STATE.try_lock() else {
            RETRY.store(session, Ordering::Release);
            poke();
            return Err(Error::Busy);
        };
        let Some(s) = guard.as_mut() else {
            return Err(Error::Unavailable);
        };
        if s.session != session || s.closing {
            return Err(Error::Unavailable);
        }
        if s.poisoned {
            return Err(Error::Io);
        }
        if s.busy || s.admitted.is_some() {
            return Err(Error::Busy);
        }
        if c.value().len() > s.capacity {
            return Err(Error::InvalidValue);
        }
        s.admitted = Some(c);
        s.busy = true;
        drop(guard);
        poke();
        Ok(())
    }
    fn poll(session: SessionId) -> Option<BackendEvent> {
        let session = session.get();

        let Ok(mut guard) = STATE.try_lock() else {
            poke();
            return None;
        };
        let s = guard.as_mut()?;
        if s.session != session {
            return None;
        }
        let e = if s.fault {
            s.fault = false;
            Some(BackendEvent::Fault)
        } else {
            s.events.pop_front()
        };
        let retry = e.is_some() && s.poisoned;
        drop(guard);
        if retry {
            poke();
        }
        e
    }
    fn recover(session: SessionId, operation: OperationId) {
        let session = session.get();
        let operation = operation.get();

        RECOVER_OP.store(operation, Ordering::Release);
        RECOVER_SESSION.store(session, Ordering::Release);
        poke();
    }
    fn close(session: SessionId) {
        let session = session.get();

        CLOSE.store(session, Ordering::Release);
        poke();
    }
}

#[cfg(all(test, native_witness))]
#[path = "../../../../../tests/host/radio_shims/gatt_macos.rs"]
mod witness;
