// Compatibility calls for the C SDK witnesses only.
use crate::gatt::{Failure as DeviceFailure, Reply, Value};
use crate::providers::gatt_backend::Command;
use crate::{gatt::Peer, PeerId, Uuid};
use std::collections::BTreeMap;
use std::sync::{
    atomic::{AtomicBool, Ordering as AtomicOrdering},
    Arc, Mutex,
};
mod wire {
    include!("gatt_wire.rs");
}
use wire::{RawCommand, RawEvent};
type Callback = unsafe extern "C" fn(*mut core::ffi::c_void);
struct Capture {
    wake: Callback,
    release: Callback,
    arg: usize,
    owned: AtomicBool,
}
impl Drop for Capture {
    fn drop(&mut self) {
        if self.owned.load(AtomicOrdering::Acquire) {
            unsafe {
                (self.release)(self.arg as *mut core::ffi::c_void);
            }
        }
    }
}
static COMMANDS: Mutex<BTreeMap<(u64, u64), Kind>> = Mutex::new(BTreeMap::new());
fn result_code(result: Result<(), Error>) -> i32 {
    result.map_or_else(code, |()| 0)
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_open(
    session: u64,
    capacity: u16,
    wake: Option<Callback>,
    release: Option<Callback>,
    arg: *mut core::ffi::c_void,
) -> i32 {
    let (Some(session), Some(wake), Some(release)) = (SessionId::new(session), wake, release)
    else {
        return code(Error::InvalidValue);
    };
    if capacity as usize > CAPACITY {
        return code(Error::InvalidValue);
    }
    let capture = Arc::new(Capture {
        wake,
        release,
        arg: arg as usize,
        owned: AtomicBool::new(false),
    });
    let retained = capture.clone();
    let owned = Wake::new(move || {
        let _keep_alive = &retained;
        unsafe {
            (retained.wake)(retained.arg as *mut core::ffi::c_void);
        }
    });
    let result = Executor::open(session, capacity as usize, owned);
    capture.owned.store(result.is_ok(), AtomicOrdering::Release);
    result_code(result)
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_submit(session: u64, raw: *const RawCommand) -> i32 {
    let Some(session) = SessionId::new(session) else {
        return code(Error::InvalidValue);
    };
    if raw.is_null() {
        return code(Error::InvalidValue);
    }
    let raw = unsafe { *raw };
    if raw.operation == 0 || raw.length as usize > CAPACITY {
        return code(Error::InvalidValue);
    }
    let command = match raw.kind {
        1 => {
            let scan = SessionId::new(raw.peer_session).unwrap_or(session);
            Command::Connect {
                peer: Peer {
                    session: scan,
                    id: PeerId(raw.peer_id),
                },
                identity: Some(raw.service),
            }
        }
        2 => Command::Discover {
            service: Uuid(raw.service),
            characteristic: Uuid(raw.characteristic),
        },
        3 => Command::Read { handle: raw.handle },
        4 => Command::Write {
            handle: raw.handle,
            value: Value::new(&raw.data[..raw.length as usize]).unwrap(),
        },
        5 => Command::Disconnect,
        _ => return code(Error::InvalidValue),
    };
    let request = Operation {
        operation: raw.operation,
        command,
    };
    let key = (session.get(), raw.operation);
    let old = COMMANDS.lock().unwrap().insert(key, request.kind());
    let result = Executor::submit(session, request);
    if result.is_err() {
        let mut map = COMMANDS.lock().unwrap();
        if let Some(old) = old {
            map.insert(key, old);
        } else {
            map.remove(&key);
        }
    }
    result_code(result)
}
fn kind_code(kind: Kind) -> u32 {
    match kind {
        Kind::Idle => 0,
        Kind::Connect => 1,
        Kind::Discover => 2,
        Kind::Read => 3,
        Kind::Write => 4,
        Kind::Disconnect => 5,
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_poll(session: u64, out: *mut RawEvent) -> i32 {
    let Some(session) = SessionId::new(session) else {
        return 0;
    };
    if out.is_null() {
        return 0;
    }
    let Some(event) = Executor::poll(session) else {
        return 0;
    };
    let mut raw = RawEvent::EMPTY;
    match event {
        BackendEvent::Completed { operation, result } => {
            raw.operation = operation;
            raw.kind = kind_code(
                COMMANDS
                    .lock()
                    .unwrap()
                    .remove(&(session.get(), operation))
                    .unwrap_or(Kind::Idle),
            );
            match result {
                Ok(reply) => match reply {
                    Reply::Connected => raw.kind = 1,
                    Reply::Discovered { handle } => {
                        raw.kind = 2;
                        raw.handle = handle.get();
                    }
                    Reply::Read(value) => {
                        raw.kind = 3;
                        raw.length = value.as_slice().len() as u16;
                        raw.data[..value.as_slice().len()].copy_from_slice(value.as_slice());
                    }
                    Reply::Written => raw.kind = 4,
                    Reply::Disconnected => raw.kind = 5,
                },
                Err(failure) => match failure {
                    DeviceFailure::Device(e) => raw.error = code(e),
                    DeviceFailure::Remote(e) => {
                        raw.kind = 9;
                        raw.error = code(e);
                    }
                    DeviceFailure::Att(att) => raw.att_error = att,
                    DeviceFailure::Disconnected => raw.error = disconnected(),
                    DeviceFailure::Cancelled => raw.error = code(Error::Io),
                    DeviceFailure::Protocol => raw.error = code(Error::InvalidValue),
                },
            }
        }
        BackendEvent::Disconnected { connection, reason } => {
            raw.operation = connection;
            raw.kind = if reason.is_some() { 6 } else { 8 };
            raw.reason = reason.unwrap_or(0);
        }
        BackendEvent::Quiescent { recovery } => {
            raw.operation = recovery;
            raw.kind = 7;
        }
        BackendEvent::Fault => {}
    }
    unsafe {
        *out = raw;
    }
    1
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_recover(session: u64, operation: u64) {
    if let (Some(session), Some(operation)) = (SessionId::new(session), OperationId::new(operation))
    {
        Executor::recover(session, operation);
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_close(session: u64) {
    if let Some(session) = SessionId::new(session) {
        Executor::close(session);
    }
}
