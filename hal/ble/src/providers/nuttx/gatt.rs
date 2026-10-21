//! NuttX GATT ownership, discovery, buffering and recovery in Rust.
//! Native callbacks and this dispatcher are serialized on LPWORK. Critical
//! sections protect only bounded state copies; native operations run outside.
#![allow(unsafe_code)]
use crate::providers::raw_gatt::{RawCommand, RawEvent, CAPACITY};
use core::{cell::UnsafeCell, ffi::c_void};
type Notify = unsafe extern "C" fn(*mut c_void);
unsafe extern "C" {
    fn nxrs_gatt_lock() -> usize;
    fn nxrs_gatt_unlock(flags: usize);
    fn nxrs_gatt_schedule(ms: u32) -> i32;
    fn nxrs_gatt_unschedule();
    fn nxrs_gatt_error(kind: u32) -> i32;
    fn nxrs_gatt_register() -> i32;
    fn nxrs_gatt_unregister() -> i32;
    fn nxrs_gatt_lookup(address: *const u8, kind: u8) -> *mut c_void;
    fn nxrs_gatt_create(address: *const u8, kind: u8) -> *mut c_void;
    fn nxrs_gatt_release_conn(conn: *mut c_void);
    fn nxrs_gatt_conn_quiet(conn: *mut c_void) -> i32;
    fn nxrs_gatt_cancel(conn: *mut c_void);
    fn nxrs_gatt_disconnect(conn: *mut c_void) -> i32;
    fn nxrs_gatt_mtu(conn: *mut c_void) -> u16;
    fn nxrs_gatt_discover(
        conn: *mut c_void,
        characteristic: i32,
        start: u16,
        end: u16,
        uuid: *const u8,
    ) -> i32;
    fn nxrs_gatt_read(conn: *mut c_void, handle: u16, offset: u16) -> i32;
    fn nxrs_gatt_write(conn: *mut c_void, handle: u16, data: *const u8, length: u16) -> i32;
    fn nxrs_ble_peer_lookup(session: u64, id: u64, address: *mut u8, kind: *mut u8) -> i32;
}
fn errno(kind: u32) -> i32 {
    unsafe { nxrs_gatt_error(kind) }
}
fn schedule(ms: u32) -> i32 {
    unsafe { nxrs_gatt_schedule(ms) }
}
struct State {
    active: bool,
    registered: bool,
    session: u64,
    capacity: u16,
    wake: Option<Notify>,
    release: Option<Notify>,
    arg: *mut c_void,
    command: RawCommand,
    result: RawEvent,
    events: [RawEvent; 4],
    head: usize,
    count: usize,
    conn: *mut c_void,
    peer: [u8; 6],
    peer_type: u8,
    connection: u64,
    recovery: u64,
    service_start: u16,
    service_end: u16,
    found_handle: u16,
    found: usize,
    characteristic: bool,
    read_offset: u16,
    retire_delay: u32,
    busy: bool,
    issued: bool,
    result_ready: bool,
    disconnected: bool,
    loss_published: bool,
    cancelled: bool,
    disconnect_sent: bool,
    closing: bool,
    recovering: bool,
    poisoned: bool,
    reason: u8,
}
impl State {
    const fn empty() -> Self {
        Self {
            active: false,
            registered: false,
            session: 0,
            capacity: 0,
            wake: None,
            release: None,
            arg: core::ptr::null_mut(),
            command: RawCommand::EMPTY,
            result: RawEvent::EMPTY,
            events: [RawEvent::EMPTY; 4],
            head: 0,
            count: 0,
            conn: core::ptr::null_mut(),
            peer: [0; 6],
            peer_type: 0,
            connection: 0,
            recovery: 0,
            service_start: 0,
            service_end: 0,
            found_handle: 0,
            found: 0,
            characteristic: false,
            read_offset: 0,
            retire_delay: 0,
            busy: false,
            issued: false,
            result_ready: false,
            disconnected: false,
            loss_published: false,
            cancelled: false,
            disconnect_sent: false,
            closing: false,
            recovering: false,
            poisoned: false,
            reason: 0,
        }
    }
    fn complete(&mut self, error: i32, att: u8) {
        if !self.busy || self.result_ready || self.recovering || self.closing {
            return;
        }
        self.result.error = error;
        self.result.att_error = att;
        self.result_ready = true;
        schedule(0);
    }
    fn owns(&self, conn: *mut c_void) -> bool {
        self.active && !conn.is_null() && conn == self.conn
    }
}
struct Cell(UnsafeCell<State>);
// SAFETY: every access is scoped to NuttX's interrupt/SMP critical section.
unsafe impl Sync for Cell {}
static STATE: Cell = Cell(UnsafeCell::new(State::empty()));
struct Critical(usize);
impl Drop for Critical {
    fn drop(&mut self) {
        unsafe { nxrs_gatt_unlock(self.0) };
    }
}
fn state<T>(f: impl FnOnce(&mut State) -> T) -> T {
    let _guard = Critical(unsafe { nxrs_gatt_lock() });
    // No reference escapes the critical section. Native SDK calls that can
    // invoke callbacks are never made while this mutable reference exists.
    f(unsafe { &mut *STATE.0.get() })
}
fn publish(event: RawEvent) {
    let notify = state(|s| {
        if s.count == s.events.len() {
            s.poisoned = true;
        } else {
            s.events[(s.head + s.count) % 4] = event;
            s.count += 1;
            if (1..=5).contains(&event.kind) {
                s.busy = false;
                s.result_ready = false;
                if event.kind == 5 && event.error == 0 {
                    s.disconnected = false;
                    s.loss_published = false;
                }
            }
            if event.kind == 7 {
                s.busy = false;
                s.result_ready = false;
                s.recovery = 0;
                s.recovering = false;
                s.disconnected = false;
                s.loss_published = false;
            }
        }
        (s.wake, s.arg)
    });
    if let (Some(wake), arg) = notify {
        unsafe { wake(arg) };
    }
}
fn retire() -> bool {
    let (conn, cancel) = state(|s| {
        let cancel = !s.cancelled;
        s.cancelled = true;
        (s.conn, cancel)
    });
    if conn.is_null() {
        return true;
    }
    if cancel {
        unsafe { nxrs_gatt_cancel(conn) };
    }
    if unsafe { nxrs_gatt_conn_quiet(conn) } != 0 {
        unsafe { nxrs_gatt_release_conn(conn) };
        state(|s| s.conn = core::ptr::null_mut());
        return true;
    }
    if state(|s| !s.disconnect_sent) {
        let result = unsafe { nxrs_gatt_disconnect(conn) };
        if result == 0 || result == -errno(3) {
            state(|s| s.disconnect_sent = true);
        }
    }
    let delay = state(|s| {
        s.retire_delay = if s.retire_delay == 0 {
            10
        } else {
            (s.retire_delay * 2).min(1000)
        };
        s.retire_delay
    });
    schedule(delay);
    false
}
#[no_mangle]
pub(crate) extern "C" fn nxrs_gatt_dispatch_rust(_: *mut c_void) {
    if state(|s| s.closing || s.recovering) {
        if !retire() {
            return;
        }
        if state(|s| s.closing) {
            if state(|s| s.registered) && unsafe { nxrs_gatt_unregister() } != 0 {
                schedule(1000);
                return;
            }
            unsafe { nxrs_gatt_unschedule() };
            let (release, arg) = state(|s| {
                let out = (s.release, s.arg);
                *s = State::empty();
                out
            });
            if let Some(release) = release {
                unsafe { release(arg) };
            }
        } else {
            let event = state(|s| RawEvent {
                operation: s.recovery,
                kind: 7,
                ..RawEvent::EMPTY
            });
            publish(event);
        }
        return;
    }
    if !state(|s| s.registered) {
        let result = unsafe { nxrs_gatt_register() };
        state(|s| {
            if result == 0 {
                s.registered = true
            } else {
                s.complete(if result < 0 { -result } else { errno(5) }, 0)
            }
        });
    }
    if let Some(event) = state(|s| {
        if s.busy && s.result_ready {
            let event = RawEvent {
                operation: s.command.operation,
                kind: s.command.kind,
                ..s.result
            };
            if s.disconnected {
                schedule(0);
            }
            Some(event)
        } else {
            None
        }
    }) {
        publish(event);
        return;
    }
    if state(|s| s.disconnected) {
        if state(|s| s.busy && s.command.kind == 5) {
            if retire() {
                state(|s| s.complete(0, 0));
            }
        } else if let Some(event) = state(|s| {
            if s.loss_published {
                None
            } else {
                s.loss_published = true;
                Some(RawEvent {
                    operation: s.connection,
                    kind: 6,
                    reason: s.reason,
                    ..RawEvent::EMPTY
                })
            }
        }) {
            publish(event);
        }
        return;
    }
    let action = state(|s| {
        if !s.busy || s.issued || !s.registered {
            return None;
        }
        s.issued = true;
        Some((
            s.command,
            s.conn,
            s.peer,
            s.peer_type,
            s.characteristic,
            s.service_start,
            s.service_end,
            s.read_offset,
        ))
    });
    let Some((cmd, conn, peer, kind, characteristic, start, end, offset)) = action else {
        return;
    };
    let result = match cmd.kind {
        1 => {
            let existing = unsafe { nxrs_gatt_lookup(peer.as_ptr(), kind) };
            if !existing.is_null() {
                unsafe { nxrs_gatt_release_conn(existing) };
                state(|s| s.complete(errno(2), 0));
                return;
            }
            state(|s| s.connection = cmd.operation);
            let created = unsafe { nxrs_gatt_create(peer.as_ptr(), kind) };
            state(|s| {
                s.conn = created;
                if created.is_null() {
                    s.complete(errno(6), 0);
                }
            });
            return;
        }
        2 => {
            state(|s| s.found = 0);
            let uuid = if characteristic {
                cmd.characteristic
            } else {
                cmd.service
            };
            unsafe {
                nxrs_gatt_discover(
                    conn,
                    i32::from(characteristic),
                    if characteristic { start + 1 } else { 1 },
                    if characteristic { end } else { u16::MAX },
                    uuid.as_ptr(),
                )
            }
        }
        3 => unsafe { nxrs_gatt_read(conn, cmd.handle, offset) },
        4 => {
            let mtu = unsafe { nxrs_gatt_mtu(conn) };
            if mtu < 3 || cmd.length > mtu - 3 {
                state(|s| s.complete(errno(7), 0));
                return;
            }
            // Stable storage is retained until completion or native quiescence.
            let data = state(|s| s.command.data.as_ptr());
            unsafe { nxrs_gatt_write(conn, cmd.handle, data, cmd.length) }
        }
        5 => unsafe { nxrs_gatt_disconnect(conn) },
        _ => -errno(1),
    };
    if result != 0 {
        state(|s| s.complete(if result < 0 { -result } else { errno(5) }, 0));
    }
}
#[no_mangle]
pub(crate) extern "C" fn nxrs_gatt_connected_rust(conn: *mut c_void) {
    state(|s| {
        if s.owns(conn) {
            if s.recovering || s.closing {
                s.cancelled = false;
                s.disconnect_sent = false;
            }
            if s.command.kind == 1 {
                s.complete(0, 0);
            }
            schedule(0);
        }
    });
}
#[no_mangle]
pub(crate) extern "C" fn nxrs_gatt_disconnected_rust(conn: *mut c_void, reason: u8) {
    state(|s| {
        if s.owns(conn) {
            s.disconnected = true;
            s.reason = reason;
            schedule(0);
        }
    });
}
#[no_mangle]
pub(crate) extern "C" fn nxrs_gatt_discovered_rust(
    handle: u16,
    end: u16,
    value: u16,
    valid: i32,
) -> u8 {
    state(|s| {
        if s.recovering || s.closing {
            return 0;
        }
        s.found += 1;
        if !s.characteristic {
            s.service_start = handle;
            s.service_end = end;
        } else if valid == 0 || value <= handle || value > s.service_end {
            s.result.error = errno(10);
        } else {
            s.found_handle = value;
        }
        1
    })
}
#[no_mangle]
pub(crate) extern "C" fn nxrs_gatt_discovery_done_rust(att: u8) {
    state(|s| {
        if s.recovering || s.closing {
            return;
        }
        if att != 0 && att != 0x0a {
            s.complete(0, att);
            return;
        }
        if s.result.error != 0 || s.found != 1 {
            s.complete(
                if s.result.error != 0 {
                    s.result.error
                } else {
                    errno(if s.found > 1 { 9 } else { 8 })
                },
                0,
            );
        } else if !s.characteristic {
            if s.service_start >= s.service_end {
                s.complete(errno(8), 0);
                return;
            }
            s.characteristic = true;
            s.issued = false;
            schedule(0);
        } else {
            s.result.handle = s.found_handle;
            s.complete(0, 0);
        }
    });
}
#[no_mangle]
pub(crate) extern "C" fn nxrs_gatt_written_rust(conn: *mut c_void, att: u8) {
    state(|s| {
        if s.owns(conn) {
            s.complete(0, att);
        }
    });
}
#[no_mangle]
pub(crate) unsafe extern "C" fn nxrs_gatt_read_rust(
    conn: *mut c_void,
    error: i32,
    data: *const u8,
    length: u16,
) {
    if !state(|s| s.owns(conn) && !s.recovering && !s.closing) {
        return;
    }
    let mtu = unsafe { nxrs_gatt_mtu(conn) };
    state(|s| {
        if !s.owns(conn) || s.recovering || s.closing {
            return;
        }
        if error != 0 {
            s.complete(
                if error < 0 { -error } else { 0 },
                if error > 0 { error as u8 } else { 0 },
            );
            return;
        }
        if mtu < 23 {
            s.complete(errno(3), 0);
            return;
        }
        let payload = mtu - 1;
        if (length != 0 && data.is_null())
            || length > payload
            || length > s.capacity - s.read_offset
        {
            s.complete(errno(1), 0);
            return;
        }
        if length != 0 {
            unsafe {
                core::ptr::copy_nonoverlapping(
                    data,
                    s.result.data.as_mut_ptr().add(s.read_offset as usize),
                    length as usize,
                )
            };
        }
        s.read_offset += length;
        s.result.length = s.read_offset;
        if length == payload {
            s.issued = false;
            schedule(0);
        } else {
            s.complete(0, 0);
        }
    });
}
#[no_mangle]
pub(crate) extern "C" fn nxrs_ble_gatt_open(
    session: u64,
    capacity: u16,
    wake: Option<Notify>,
    release: Option<Notify>,
    arg: *mut c_void,
) -> i32 {
    state(|s| {
        if session == 0 || capacity > CAPACITY as u16 || wake.is_none() || release.is_none() {
            return errno(1);
        }
        if s.active {
            return errno(2);
        }
        *s = State {
            active: true,
            session,
            capacity,
            wake,
            release,
            arg,
            ..State::empty()
        };
        0
    })
}
#[no_mangle]
pub(crate) unsafe extern "C" fn nxrs_ble_gatt_submit(
    session: u64,
    command: *const RawCommand,
) -> i32 {
    if command.is_null() {
        return errno(1);
    }
    let cmd = unsafe { *command };
    state(|s| {
        if !s.active || session != s.session || s.closing || s.poisoned {
            return errno(4);
        }
        if s.busy || s.recovering || s.count != 0 {
            return errno(2);
        }
        if cmd.operation == 0
            || !(1..=5).contains(&cmd.kind)
            || cmd.length > s.capacity
            || cmd.operation <= s.command.operation
        {
            return errno(1);
        }
        if (cmd.kind == 1) != s.conn.is_null() || s.disconnected {
            return errno(3);
        }
        if cmd.kind == 1 {
            let result = unsafe {
                nxrs_ble_peer_lookup(
                    cmd.peer_session,
                    cmd.peer_id,
                    s.peer.as_mut_ptr(),
                    &mut s.peer_type,
                )
            };
            if result != 0 {
                return if result == errno(2) {
                    errno(2)
                } else {
                    errno(1)
                };
            }
        }
        s.command = cmd;
        s.result = RawEvent::EMPTY;
        s.busy = true;
        s.issued = false;
        s.result_ready = false;
        s.cancelled = false;
        s.disconnect_sent = false;
        s.characteristic = false;
        s.read_offset = 0;
        s.retire_delay = 0;
        let result = schedule(0);
        if result != 0 {
            s.busy = false;
        }
        result.abs()
    })
}
#[no_mangle]
pub(crate) extern "C" fn nxrs_ble_gatt_recover(session: u64, operation: u64) {
    state(|s| {
        if s.active && session == s.session && operation != 0 && !s.closing {
            if !s.recovering {
                s.recovery = operation;
                s.recovering = true;
            }
            schedule(0);
        }
    });
}
#[no_mangle]
pub(crate) unsafe extern "C" fn nxrs_ble_gatt_poll(session: u64, event: *mut RawEvent) -> i32 {
    state(|s| {
        if s.active && session == s.session && !event.is_null() && s.count != 0 {
            unsafe { *event = s.events[s.head] };
            s.head = (s.head + 1) % 4;
            s.count -= 1;
            1
        } else {
            0
        }
    })
}
#[no_mangle]
pub(crate) extern "C" fn nxrs_ble_gatt_close(session: u64) {
    state(|s| {
        if s.active && session == s.session && !s.closing {
            s.closing = true;
            schedule(0);
        }
    });
}
