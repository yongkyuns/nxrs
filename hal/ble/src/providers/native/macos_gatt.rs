//! CoreBluetooth GATT behavior. All SDK operations execute on its serial queue.
//! The front door uses try_lock; retirement signals never need that lock.
use crate::providers::raw_gatt::{RawCommand, RawEvent, CAPACITY};
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
    fn nxrs_mac_gatt_initialize();
    fn nxrs_mac_gatt_allocate();
    fn nxrs_mac_gatt_poke();
    fn nxrs_mac_gatt_defer();
    fn nxrs_mac_gatt_errno(kind: u32) -> i32;
    fn nxrs_mac_gatt_authorization() -> i32;
    fn nxrs_mac_gatt_manager();
    fn nxrs_mac_gatt_manager_state() -> u32;
    fn nxrs_mac_gatt_object(field: u32, index: usize) -> *mut c_void;
    fn nxrs_mac_gatt_count(field: u32) -> usize;
    fn nxrs_mac_gatt_identity(object: *mut c_void, bytes: *mut u8);
    fn nxrs_mac_gatt_uuid(
        object: *mut c_void,
        characteristic: bool,
        bytes: *mut *const u8,
        len: *mut usize,
    );
    fn nxrs_mac_gatt_retrieve(identity: *const u8, index: usize, count: *mut usize) -> *mut c_void;
    fn nxrs_mac_gatt_scan();
    fn nxrs_mac_gatt_stop_scan();
    fn nxrs_mac_gatt_connect(peripheral: *mut c_void);
    fn nxrs_mac_gatt_connected() -> bool;
    fn nxrs_mac_gatt_cancel();
    fn nxrs_mac_gatt_services(uuid: *const u8);
    fn nxrs_mac_gatt_characteristics(service: *mut c_void, uuid: *const u8);
    fn nxrs_mac_gatt_pin(attribute: *mut c_void);
    fn nxrs_mac_gatt_properties(attribute: *mut c_void) -> u32;
    fn nxrs_mac_gatt_max_write() -> usize;
    fn nxrs_mac_gatt_read(attribute: *mut c_void);
    fn nxrs_mac_gatt_write(attribute: *mut c_void, data: *const u8, len: usize);
    fn nxrs_mac_gatt_detach(release: bool);
}
type Notify = unsafe extern "C" fn(*mut c_void);
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
    wake: Option<Notify>,
    release: Option<Notify>,
    arg: usize,
    admitted: Option<RawCommand>,
    command: RawCommand,
    result: Option<RawEvent>,
    events: VecDeque<RawEvent>,
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
    unsafe { nxrs_mac_gatt_errno(kind) }
}
fn poke() {
    unsafe { nxrs_mac_gatt_poke() }
}
fn notify(value: Option<(Notify, usize)>) {
    if let Some((f, arg)) = value {
        unsafe { f(arg as *mut c_void) }
    }
}
impl State {
    fn wake(&self) -> Option<(Notify, usize)> {
        self.wake.map(|f| (f, self.arg))
    }
    fn schedule(&mut self) {
        if !self.flush {
            self.flush = true;
            unsafe { nxrs_mac_gatt_defer() }
        }
    }
    fn complete(&mut self, error: i32, att: u8) {
        if self.result.is_some() || matches!(self.phase, Phase::Idle | Phase::Retiring) {
            return;
        }
        let mut result = RawEvent::EMPTY;
        result.operation = self.command.operation;
        result.kind = self.command.kind;
        result.error = error;
        result.att_error = att;
        self.result = Some(result);
        self.phase = Phase::Idle;
        self.schedule();
    }
    fn retire(&mut self) {
        unsafe { nxrs_mac_gatt_stop_scan() };
        if self.issued && !self.quiet {
            unsafe { nxrs_mac_gatt_cancel() }
        } else {
            self.quiet = true;
            self.schedule();
        }
    }
    fn identity(&self, object: *mut c_void) -> bool {
        let mut bytes = [0; 16];
        unsafe { nxrs_mac_gatt_identity(object, bytes.as_mut_ptr()) };
        bytes == self.command.service
    }
    fn connect(&mut self) {
        if self.phase != Phase::Connecting || self.issued {
            return;
        }
        let mut count = 0;
        let first = unsafe { nxrs_mac_gatt_retrieve(self.command.service.as_ptr(), 0, &mut count) };
        let mut found = None;
        for i in 0..count {
            let p = if i == 0 {
                first
            } else {
                unsafe { nxrs_mac_gatt_retrieve(self.command.service.as_ptr(), i, &mut 0) }
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
            unsafe { nxrs_mac_gatt_connect(p) }
        } else {
            unsafe { nxrs_mac_gatt_scan() }
        }
    }
    fn start(&mut self) {
        if self.command.kind == 1 {
            self.phase = Phase::Connecting;
            self.quiet = true;
            let e = unsafe { nxrs_mac_gatt_authorization() };
            if e != 0 {
                self.complete(e, 0);
                return;
            }
            unsafe { nxrs_mac_gatt_manager() };
            if unsafe { nxrs_mac_gatt_manager_state() } == 0 {
                self.connect();
            }
            return;
        }
        if !unsafe { nxrs_mac_gatt_connected() } {
            self.phase = Phase::Reading;
            self.complete(err(4), 0);
            return;
        }
        match self.command.kind {
            5 => {
                self.phase = Phase::Disconnecting;
                self.retire();
            }
            2 => {
                self.phase = Phase::Services;
                unsafe { nxrs_mac_gatt_services(self.command.service.as_ptr()) }
            }
            _ => {
                self.phase = if self.command.kind == 3 {
                    Phase::Reading
                } else {
                    Phase::Writing
                };
                let Some(index) = usize::from(self.command.handle).checked_sub(1) else {
                    self.complete(err(1), 0);
                    return;
                };
                let Some(&attribute) = self.attributes.get(index) else {
                    self.complete(err(1), 0);
                    return;
                };
                let attribute = attribute as *mut c_void;
                let properties = unsafe { nxrs_mac_gatt_properties(attribute) };
                if properties & 1 != 0
                    || properties & if self.command.kind == 3 { 2 } else { 4 } == 0
                {
                    self.complete(err(9), 0);
                    return;
                }
                if self.command.kind == 3 {
                    unsafe { nxrs_mac_gatt_read(attribute) }
                } else {
                    if usize::from(self.command.length) > unsafe { nxrs_mac_gatt_max_write() } {
                        self.complete(err(9), 0);
                        return;
                    }
                    unsafe {
                        nxrs_mac_gatt_write(
                            attribute,
                            self.command.data.as_ptr(),
                            self.command.length.into(),
                        )
                    }
                }
            }
        }
    }
    fn matched(&self, object: *mut c_void, characteristic: bool, target: [u8; 16]) -> bool {
        let mut data = std::ptr::null();
        let mut len = 0;
        unsafe { nxrs_mac_gatt_uuid(object, characteristic, &mut data, &mut len) };
        len != 0
            && super::macos_uuid::canonical(unsafe { std::slice::from_raw_parts(data, len) })
                == Some(target)
    }
    fn discovery(&mut self, characteristic: bool) {
        let field = if characteristic { 6 } else { 5 };
        let target = if characteristic {
            self.command.characteristic
        } else {
            self.command.service
        };
        let mut found = None;
        for i in 0..unsafe { nxrs_mac_gatt_count(field) } {
            let object = unsafe { nxrs_mac_gatt_object(field, i) };
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
            unsafe { nxrs_mac_gatt_characteristics(object, self.command.characteristic.as_ptr()) };
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
            unsafe { nxrs_mac_gatt_pin(object) };
            i
        };
        self.complete(0, 0);
        self.result.as_mut().unwrap().handle = (index + 1) as u16;
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_mac_gatt_control_rust() {
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
unsafe extern "C" fn nxrs_mac_gatt_flush_rust() {
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
            let mut e = RawEvent::EMPTY;
            e.operation = s.connection;
            e.kind = 8;
            Some((e, false, 2))
        } else if s.quiet && matches!(s.phase, Phase::Retiring | Phase::Disconnecting) {
            unsafe { nxrs_mac_gatt_detach(s.closing) };
            s.issued = false;
            s.attributes.clear();
            if s.closing {
                let release = s.release.map(|f| (f, s.arg));
                *guard = None;
                drop(guard);
                notify(release);
                return;
            }
            let mut e = RawEvent::EMPTY;
            if s.phase == Phase::Disconnecting {
                e.operation = s.command.operation;
                e.kind = 5;
            } else {
                e.operation = s.recovery;
                e.kind = 7;
            }
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
unsafe extern "C" fn nxrs_mac_gatt_event_rust(
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
    if kind <= 5 && manager != unsafe { nxrs_mac_gatt_object(1, 0) } {
        return;
    }
    if kind >= 3 && peripheral != unsafe { nxrs_mac_gatt_object(2, 0) } {
        return;
    }
    match kind {
        1 => {
            let status = unsafe { nxrs_mac_gatt_manager_state() };
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
            unsafe { nxrs_mac_gatt_stop_scan() };
            s.issued = true;
            s.quiet = false;
            unsafe { nxrs_mac_gatt_connect(peripheral) }
        }
        3 => {
            if matches!(s.phase, Phase::Retiring | Phase::Disconnecting) {
                unsafe { nxrs_mac_gatt_cancel() };
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
                        || object != unsafe { nxrs_mac_gatt_object(3, 0) }))
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
            if object != unsafe { nxrs_mac_gatt_object(4, 0) }
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
                s.complete(0, 0);
                let r = s.result.as_mut().unwrap();
                r.length = len as u16;
                if len != 0 {
                    unsafe { std::ptr::copy_nonoverlapping(data, r.data.as_mut_ptr(), len) }
                }
            } else {
                s.complete(0, 0);
            }
        }
        _ => {}
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_open(
    session: u64,
    capacity: u16,
    wake: Option<Notify>,
    release: Option<Notify>,
    arg: *mut c_void,
) -> i32 {
    if session == 0 || usize::from(capacity) > CAPACITY || wake.is_none() || release.is_none() {
        return err(1);
    }
    unsafe { nxrs_mac_gatt_initialize() };
    let Ok(mut state) = STATE.try_lock() else {
        return err(2);
    };
    if state.is_some() {
        return err(2);
    }
    unsafe { nxrs_mac_gatt_allocate() };
    *state = Some(State {
        session,
        capacity: capacity.into(),
        wake,
        release,
        arg: arg as usize,
        admitted: None,
        command: RawCommand::EMPTY,
        result: None,
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
    0
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_submit(session: u64, command: *const RawCommand) -> i32 {
    if command.is_null() {
        return err(1);
    }
    let c = unsafe { *command };
    if c.operation == 0 || !(1..=5).contains(&c.kind) || usize::from(c.length) > CAPACITY {
        return err(1);
    }
    let Ok(mut guard) = STATE.try_lock() else {
        RETRY.store(session, Ordering::Release);
        poke();
        return err(2);
    };
    let Some(s) = guard.as_mut() else {
        return err(4);
    };
    if s.session != session || s.closing {
        return err(4);
    }
    if s.poisoned {
        return err(3);
    }
    if s.busy || s.admitted.is_some() {
        return err(2);
    }
    if usize::from(c.length) > s.capacity {
        return err(5);
    }
    s.admitted = Some(c);
    s.busy = true;
    drop(guard);
    poke();
    0
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_poll(session: u64, event: *mut RawEvent) -> i32 {
    if event.is_null() {
        return 0;
    }
    let Ok(mut guard) = STATE.try_lock() else {
        poke();
        return 0;
    };
    let Some(s) = guard.as_mut() else {
        return 0;
    };
    if s.session != session {
        return 0;
    }
    let e = if s.fault {
        s.fault = false;
        Some(RawEvent::EMPTY)
    } else {
        s.events.pop_front()
    };
    let retry = e.is_some() && s.poisoned;
    let found = e.is_some();
    if let Some(e) = e {
        unsafe { event.write(e) }
    }
    drop(guard);
    if retry {
        poke();
    }
    i32::from(found)
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_recover(session: u64, op: u64) {
    RECOVER_OP.store(op, Ordering::Release);
    RECOVER_SESSION.store(session, Ordering::Release);
    poke();
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_close(session: u64) {
    CLOSE.store(session, Ordering::Release);
    poke();
}
#[cfg(test)]
#[no_mangle]
extern "C" fn nxrs_mac_gatt_test_session() -> u64 {
    STATE.lock().unwrap().as_ref().map_or(0, |s| s.session)
}
#[cfg(test)]
#[no_mangle]
unsafe extern "C" fn nxrs_mac_gatt_test_contend(inject: bool) {
    let mut g = STATE.lock().unwrap();
    let s = g.as_mut().unwrap();
    let session = s.session;
    if inject {
        let mut e = RawEvent::EMPTY;
        e.kind = 1;
        e.operation = 1;
        s.events.push_back(e);
    }
    let mut c = RawCommand::EMPTY;
    c.kind = 1;
    c.operation = 1;
    let mut e = RawEvent::EMPTY;
    assert_eq!(unsafe { nxrs_ble_gatt_submit(session, &c) }, err(2));
    assert_eq!(unsafe { nxrs_ble_gatt_poll(session, &mut e) }, 0);
}
#[cfg(test)]
#[no_mangle]
extern "C" fn nxrs_mac_gatt_test_saturate() {
    STATE.lock().unwrap().as_mut().unwrap().events = VecDeque::from([RawEvent::EMPTY; 4]);
}
