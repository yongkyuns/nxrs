//! BlueZ GATT executor. Rust owns admission, IPC sequencing and callback retirement.
use super::{
    gio::{self, Bus, Context, Failure, Reply, G, V},
    identity::{identity, uuid},
};
use crate::gatt_backend::{
    device_error, Backend, Event as BackendEvent, Kind, Outcome, Request as Operation, Wake,
    CAPACITY,
};
use crate::{Error, OperationId, SessionId};
use std::prelude::v1::*;
use std::rc::Rc;
use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
    ffi::c_char,
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex, OnceLock,
    },
};
use std::{format, vec};
const DEVICE: &str = "org.bluez.Device1";
const SERVICE: &str = "org.bluez.GattService1";
const CHAR: &str = "org.bluez.GattCharacteristic1";
const PROPERTIES: &str = "org.freedesktop.DBus.Properties";
const OBJECTS: &str = "org.freedesktop.DBus.ObjectManager";
struct Front {
    session: u64,
    capacity: usize,
    admitted: Option<Operation>,
    events: VecDeque<BackendEvent>,
    busy: bool,
    closing: bool,
    poisoned: bool,
    fault: bool,
}
static FRONT: Mutex<Option<Front>> = Mutex::new(None);
static CLOSE: AtomicU64 = AtomicU64::new(0);
static RECOVER_SESSION: AtomicU64 = AtomicU64::new(0);
static RECOVER_OP: AtomicU64 = AtomicU64::new(0);
static RETRY: AtomicU64 = AtomicU64::new(0);
// Process-lifetime context: priority signals cannot race a freed native pointer.
static CONTROL: OnceLock<usize> = OnceLock::new();
fn poke() {
    if let Some(&context) = CONTROL.get() {
        unsafe { gio::g_main_context_wakeup(context as G) }
    }
}
fn err(kind: u32) -> i32 {
    gio::constant(kind)
}
fn error(e: &Failure, att: &mut u8) -> i32 {
    match e.class {
        1 => return err(18),
        2 => return err(13),
        3 => return err(7),
        _ => {}
    }
    match e.name.as_str() {
        "org.bluez.Error.NotConnected" => err(7),
        "org.bluez.Error.NotReady" => err(14),
        "org.bluez.Error.NotAuthorized" | "org.bluez.Error.NotPermitted" => err(13),
        "org.bluez.Error.NotSupported" => err(12),
        "org.bluez.Error.InvalidArguments"
        | "org.bluez.Error.InvalidOffset"
        | "org.bluez.Error.InvalidValueLength" => err(4),
        "org.bluez.Error.InProgress" | "org.bluez.Error.AlreadyConnected" => err(5),
        "org.bluez.Error.Failed" => {
            if let Some(text) = e
                .message
                .strip_prefix("Operation failed with ATT error: 0x")
            {
                if text.len() == 2 && text.bytes().all(|b| b.is_ascii_hexdigit()) {
                    if let Ok(code) = u8::from_str_radix(text, 16) {
                        if code != 0 {
                            *att = code;
                            return 0;
                        }
                    }
                }
            }
            err(6)
        }
        _ => err(6),
    }
}
fn required(p: &V, key: &str, kind: &str) -> Result<V, i32> {
    p.lookup(key, kind).ok_or_else(|| err(4))
}
struct Changes {
    gone: bool,
    removed: bool,
    resolved: Option<bool>,
    disconnected: bool,
}
struct Signals {
    daemon: RefCell<String>,
    device: RefCell<String>,
    changes: RefCell<Changes>,
    callbacks: Cell<usize>,
}
unsafe extern "C" fn detached(data: G) {
    let s = unsafe { &*data.cast::<Signals>() };
    s.callbacks.set(s.callbacks.get() - 1);
}
unsafe extern "C" fn signal(
    _: G,
    _: *const c_char,
    path: *const c_char,
    _: *const c_char,
    member: *const c_char,
    p: G,
    data: G,
) {
    let s = unsafe { &*data.cast::<Signals>() };
    let member = unsafe { gio::borrowed_text(member) };
    let p = unsafe { gio::borrowed_variant(p) };
    if member == "NameOwnerChanged" {
        if p.is("(sss)")
            && p.child(0).string() == *s.daemon.borrow()
            && !p.child(1).string().is_empty()
            && p.child(2).string().is_empty()
        {
            s.changes.borrow_mut().gone = true;
        }
    } else if member == "InterfacesRemoved" {
        if p.is("(oas)") && p.child(0).string() == *s.device.borrow() {
            let list = p.child(1);
            if (0..list.len()).any(|i| list.child(i).string() == DEVICE) {
                s.changes.borrow_mut().removed = true;
            }
        }
    } else if unsafe { gio::borrowed_text(path) } == *s.device.borrow()
        && p.is("(sa{sv}as)")
        && p.child(0).string() == DEVICE
    {
        let values = p.child(1);
        let mut changes = s.changes.borrow_mut();
        if values
            .lookup("Connected", "b")
            .is_some_and(|v| !v.boolean())
        {
            changes.disconnected = true;
        }
        if let Some(v) = values.lookup("ServicesResolved", "b") {
            changes.resolved = Some(v.boolean());
        }
    }
}
struct Attribute {
    path: String,
    read: bool,
    write: bool,
}
struct Actor {
    session: u64,
    capacity: usize,
    wake: Wake,
    context: Context,
    bus: Option<Bus>,
    daemon: String,
    device: String,
    signals: Rc<Signals>,
    watches: Vec<u32>,
    attributes: Vec<Attribute>,
    command: Operation,
    stop: Option<Rc<Reply>>,
    connection: u64,
    recovery: u64,
    handled: u64,
    stop_retry: i64,
    owned: bool,
    gone: bool,
    removed: bool,
    resolved: bool,
    lost: bool,
    retiring: bool,
    closing: bool,
    stop_again: bool,
}
impl Actor {
    fn refresh(&mut self) {
        let mut guard = FRONT.lock().unwrap();
        let s = guard.as_mut().unwrap();
        if CLOSE.load(Ordering::Acquire) == self.session {
            s.closing = true;
            self.closing = true;
        }
        let token = if RECOVER_SESSION.load(Ordering::Acquire) == self.session {
            RECOVER_OP.load(Ordering::Acquire)
        } else {
            0
        };
        if token != 0
            && token != self.handled
            && (token == self.command.operation
                || token == self.connection
                || s.admitted.is_some_and(|c| c.operation == token))
        {
            self.recovery = token;
            self.handled = token;
            self.retiring = true;
        }
        if self.closing {
            self.retiring = true;
        }
        if self.retiring {
            s.admitted = None;
            s.busy = true;
        }
        drop(guard);
        let mut changes = self.signals.changes.borrow_mut();
        self.gone |= changes.gone || self.bus.as_ref().is_some_and(Bus::closed);
        self.removed |= changes.removed;
        if let Some(resolved) = changes.resolved.take() {
            self.resolved = resolved;
        }
        if self.gone || self.removed || changes.disconnected {
            if self.owned && self.connection != 0 {
                self.lost = true;
            }
            self.owned = false;
        }
        changes.disconnected = false;
    }
    fn start_stop(&mut self) {
        if !self.owned || self.stop.is_some() || gio::now() < self.stop_retry {
            return;
        }
        self.stop = Some(self.bus.as_ref().unwrap().start(
            &self.daemon,
            &self.device,
            DEVICE,
            "Disconnect",
            None,
            "()",
            i32::MAX,
            &self.context,
        ));
    }
    fn collect_stop(&mut self) {
        let Some(stop) = self.stop.as_ref() else {
            return;
        };
        let Some(result) = stop.take() else {
            return;
        };
        if result.as_ref().is_ok_and(|_| true)
            || result
                .as_ref()
                .err()
                .is_some_and(|e| error(e, &mut 0) == err(7))
        {
            self.owned = self.stop_again;
            self.stop_again = false;
            if self.connection != 0 {
                self.lost = true;
            }
        } else {
            self.stop_retry = gio::now() + 250000;
        }
        self.stop = None;
    }
    fn rpc(
        &mut self,
        path: &str,
        interface: &str,
        method: &str,
        parameters: Option<V>,
        signature: &str,
        mutating: bool,
    ) -> Result<V, Failure> {
        let reply = self.bus.as_ref().unwrap().start(
            &self.daemon,
            path,
            interface,
            method,
            parameters,
            signature,
            if mutating { i32::MAX } else { 2000 },
            &self.context,
        );
        while !reply.done() {
            self.refresh();
            self.collect_stop();
            if self.retiring {
                self.start_stop();
            }
            if !reply.done() {
                self.context.iterate(true);
            }
        }
        reply.take().unwrap()
    }
    fn properties(&mut self, path: &str, interface: &str) -> Result<V, i32> {
        self.rpc(
            path,
            PROPERTIES,
            "GetAll",
            Some(V::tuple(&[V::str(interface)])),
            "(a{sv})",
            false,
        )
        .map(|v| v.child(0))
        .map_err(|e| error(&e, &mut 0))
    }
    fn objects(&mut self) -> Result<V, i32> {
        self.rpc(
            "/",
            OBJECTS,
            "GetManagedObjects",
            None,
            "(a{oa{sa{sv}}})",
            false,
        )
        .map(|v| v.child(0))
        .map_err(|e| error(&e, &mut 0))
    }
    fn open_bus(&mut self) -> Result<(), i32> {
        let bus = Bus::open().map_err(|e| error(&e, &mut 0))?;
        let daemon = bus
            .call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetNameOwner",
                Some(V::tuple(&[V::str("org.bluez")])),
                "(s)",
                2000,
            )
            .map_err(|e| error(&e, &mut 0))?
            .child(0)
            .string();
        self.daemon = daemon.clone();
        self.signals.daemon.replace(daemon);
        self.signals.callbacks.set(3);
        let data = Rc::as_ptr(&self.signals).cast_mut().cast();
        for (name, interface, member, path, arg) in [
            (
                &*self.daemon,
                PROPERTIES,
                "PropertiesChanged",
                None,
                Some(DEVICE),
            ),
            (
                "org.freedesktop.DBus",
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                Some("/org/freedesktop/DBus"),
                Some(&*self.daemon),
            ),
            (&*self.daemon, OBJECTS, "InterfacesRemoved", Some("/"), None),
        ] {
            self.watches.push(bus.subscribe(
                name,
                interface,
                member,
                path,
                arg,
                signal,
                data,
                detached,
                &self.context,
            ));
        }
        self.bus = Some(bus);
        Ok(())
    }
    fn connect(&mut self) -> Result<(), i32> {
        if self.owned || !self.device.is_empty() || self.bus.is_some() {
            return Err(err(5));
        }
        self.open_bus()?;
        let objects = self.objects()?;
        for i in 0..objects.len() {
            let entry = objects.child(i);
            let Some(values) = entry.child(1).lookup(DEVICE, "a{sv}") else {
                continue;
            };
            let path = entry.child(0).string();
            if self.command.identity() != Some(identity(&self.daemon, &path)) {
                continue;
            }
            let connected = required(&values, "Connected", "b")?.boolean();
            if connected || !self.device.is_empty() {
                return Err(err(5));
            }
            self.device = path;
        }
        if self.device.is_empty() {
            return Err(err(16));
        }
        *self.signals.device.borrow_mut() = self.device.clone();
        self.refresh();
        if self.retiring {
            return Err(err(15));
        }
        self.owned = true;
        let path = self.device.clone();
        let reply = self.rpc(&path, DEVICE, "Connect", None, "()", true);
        match reply {
            Ok(_) => {
                self.connection = self.command.operation;
                if !self.owned {
                    self.lost = true;
                }
                self.owned = true;
                if self.retiring && self.stop.is_some() {
                    self.stop_again = true;
                }
                Ok(())
            }
            Err(e) => {
                let status = error(&e, &mut 0);
                if [5, 13, 14, 12, 4].iter().any(|&kind| status == err(kind)) {
                    self.owned = false;
                }
                Err(status)
            }
        }
    }
    fn discover(&mut self, event: &mut Outcome) -> Result<(), i32> {
        let path = self.device.clone();
        let values = self.properties(&path, DEVICE)?;
        let connected = required(&values, "Connected", "b")?.boolean();
        self.resolved = required(&values, "ServicesResolved", "b")?.boolean();
        if !connected {
            self.owned = false;
            return Err(err(7));
        }
        while !self.resolved && !self.retiring && !self.gone && self.owned {
            self.context.iterate(true);
            self.refresh();
        }
        if self.retiring {
            return Err(err(15));
        }
        if !self.owned || self.gone {
            return Err(err(7));
        }
        let objects = self.objects()?;
        let service = uuid(&self.command.service());
        let characteristic = uuid(&self.command.characteristic());
        let mut selected = None;
        for i in 0..objects.len() {
            let entry = objects.child(i);
            let Some(p) = entry.child(1).lookup(SERVICE, "a{sv}") else {
                continue;
            };
            let (Some(uuid), Some(device)) = (p.lookup("UUID", "s"), p.lookup("Device", "o"))
            else {
                continue;
            };
            if device.string() != self.device || !uuid.string().eq_ignore_ascii_case(&service) {
                continue;
            }
            if selected.is_some() {
                return Err(err(9));
            }
            selected = Some(entry.child(0).string());
        }
        let selected = selected.ok_or_else(|| err(10))?;
        let mut found = None;
        for i in 0..objects.len() {
            let entry = objects.child(i);
            let Some(p) = entry.child(1).lookup(CHAR, "a{sv}") else {
                continue;
            };
            let (Some(uuid), Some(parent)) = (p.lookup("UUID", "s"), p.lookup("Service", "o"))
            else {
                continue;
            };
            if parent.string() != selected || !uuid.string().eq_ignore_ascii_case(&characteristic) {
                continue;
            }
            if found.is_some() {
                return Err(err(9));
            }
            if p.lookup("Notifying", "b").is_some_and(|v| v.boolean()) {
                return Err(err(12));
            }
            let flags = required(&p, "Flags", "as")?;
            let has = |flag| (0..flags.len()).any(|i| flags.child(i).string() == flag);
            found = Some(Attribute {
                path: entry.child(0).string(),
                read: has("read"),
                write: has("write"),
            });
        }
        let found = found.ok_or_else(|| err(10))?;
        let index = if let Some(i) = self.attributes.iter().position(|a| a.path == found.path) {
            i
        } else {
            if self.attributes.len() == 32 {
                return Err(err(11));
            }
            let i = self.attributes.len();
            self.attributes.push(found);
            i
        };
        event.handle = (index + 1) as u16;
        Ok(())
    }
    fn execute(&mut self) -> BackendEvent {
        let mut event = Outcome::EMPTY;
        let result = match self.command.kind() {
            Kind::Connect => self.connect(),
            _ if !self.owned || self.gone || self.connection == 0 => Err(err(7)),
            Kind::Disconnect => {
                self.retiring = true;
                Ok(())
            }
            Kind::Discover => self.discover(&mut event),
            _ => self.value(&mut event),
        };
        if let Err(e) = result {
            event.native_error(e, 0, err(12));
        }
        event.finish(self.command)
    }
    fn value(&mut self, event: &mut Outcome) -> Result<(), i32> {
        let Some(index) = usize::from(self.command.handle()).checked_sub(1) else {
            return Err(err(4));
        };
        let Some(attribute) = self.attributes.get(index) else {
            return Err(err(4));
        };
        let read = self.command.kind() == Kind::Read;
        if !(if read {
            attribute.read
        } else {
            attribute.write
        }) {
            return Err(err(12));
        }
        let path = attribute.path.clone();
        let options = V::dict(if read {
            vec![]
        } else {
            vec![("type", V::str("request"))]
        });
        let params = if read {
            V::tuple(&[options])
        } else {
            V::tuple(&[V::byte_array(self.command.value()), options])
        };
        match self.rpc(
            &path,
            CHAR,
            if read { "ReadValue" } else { "WriteValue" },
            Some(params),
            if read { "(ay)" } else { "()" },
            !read,
        ) {
            Ok(reply) => {
                if read {
                    let bytes = reply.child(0).bytes();
                    if bytes.len() > self.capacity {
                        return Err(err(4));
                    }
                    event.length = bytes.len();
                    event.data[..bytes.len()].copy_from_slice(&bytes);
                }
                Ok(())
            }
            Err(e) => {
                let mut att = 0;
                let code = error(&e, &mut att);
                event.native_error(code, att, err(12));
                if [
                    "NotPermitted",
                    "NotAuthorized",
                    "NotSupported",
                    "InvalidArguments",
                    "InvalidOffset",
                    "InvalidValueLength",
                    "InProgress",
                ]
                .iter()
                .any(|name| e.name == format!("org.bluez.Error.{name}"))
                {
                    event.failure = Some(crate::gatt::Failure::Remote(if code == err(12) {
                        Error::Unsupported
                    } else {
                        device_error(code)
                    }));
                }
                Ok(())
            }
        }
    }
    fn enqueue(&self, event: BackendEvent, release_busy: bool) -> bool {
        let mut guard = FRONT.lock().unwrap();
        let s = guard.as_mut().unwrap();
        let room = s.events.len() < 4;
        if room {
            s.events.push_back(event);
            if release_busy {
                s.busy = false;
            }
        } else {
            s.poisoned = true;
            s.fault = true;
        }
        drop(guard);
        self.wake.notify();
        room
    }
    fn loss(&mut self) {
        if self.lost
            && self.enqueue(
                BackendEvent::Disconnected {
                    connection: self.connection,
                    reason: None,
                },
                false,
            )
        {
            self.lost = false;
        }
    }
    fn detach(&mut self) {
        if let Some(bus) = self.bus.as_ref() {
            for id in self.watches.drain(..) {
                bus.unsubscribe(id);
            }
            while self.signals.callbacks.get() != 0 {
                self.context.iterate(true);
            }
        }
        self.bus = None;
        self.device.clear();
        self.daemon.clear();
        self.attributes.clear();
        self.stop_retry = 0;
        self.gone = false;
        self.removed = false;
        self.resolved = false;
        self.signals.changes.replace(Changes {
            gone: false,
            removed: false,
            resolved: None,
            disconnected: false,
        });
        self.signals.device.borrow_mut().clear();
    }
    fn run(mut self) {
        self.context.push();
        let timer = unsafe { gio::g_timeout_source_new(250) };
        unsafe {
            gio::g_source_set_callback(timer, tick, std::ptr::null_mut(), None);
            gio::g_source_attach(timer, self.context.0)
        };
        loop {
            self.refresh();
            self.collect_stop();
            if self.retiring {
                if self.owned && self.stop.is_none() {
                    self.start_stop();
                }
                if !self.owned && self.stop.is_none() {
                    self.loss();
                    self.detach();
                    if self.closing {
                        break;
                    }
                    let event = if self.recovery != 0 {
                        BackendEvent::Quiescent {
                            recovery: self.recovery,
                        }
                    } else {
                        BackendEvent::Completed {
                            operation: self.command.operation,
                            result: Ok(crate::gatt::Reply::Disconnected),
                        }
                    };
                    if self.enqueue(event, true) {
                        self.retiring = false;
                        self.connection = 0;
                        self.recovery = 0;
                    }
                }
            } else {
                let mut guard = FRONT.lock().unwrap();
                let s = guard.as_mut().unwrap();
                let command = s.admitted.take();
                let waiting = !s.events.is_empty()
                    || s.fault
                    || RETRY.swap(0, Ordering::AcqRel) == self.session;
                drop(guard);
                if let Some(command) = command {
                    self.command = command;
                    let event = self.execute();
                    self.refresh();
                    if self.retiring {
                        continue;
                    }
                    self.enqueue(event, true);
                }
                self.loss();
                if waiting {
                    self.wake.notify()
                }
            }
            self.context.iterate(true);
        }
        unsafe {
            gio::g_source_destroy(timer);
            gio::g_source_unref(timer)
        };
        self.context.pop();
        let mut front = FRONT.lock().unwrap();
        *front = None;
        drop(front);
        // Wake ownership drops only after subscriptions and FRONT are retired.
    }
}
unsafe extern "C" fn tick(_: G) -> i32 {
    1
}

pub(crate) struct Executor;
impl Backend for Executor {
    fn open(session: SessionId, capacity: usize, wake: Wake) -> Result<(), Error> {
        let session = session.get();

        if capacity > CAPACITY {
            return Err(Error::InvalidValue);
        }
        let Ok(mut front) = FRONT.try_lock() else {
            return Err(Error::Busy);
        };
        if front.is_some() {
            return Err(Error::Busy);
        }
        let actor_wake = wake.clone();
        let context = *CONTROL.get_or_init(|| unsafe { gio::g_main_context_new() } as usize);
        *front = Some(Front {
            session,
            capacity,
            admitted: None,
            events: VecDeque::with_capacity(4),
            busy: false,
            closing: false,
            poisoned: false,
            fault: false,
        });
        let spawn = std::thread::Builder::new()
            .name("ble-gatt".into())
            .spawn(move || {
                let context = Context::retain(context as G);
                Actor {
                    session,
                    capacity,
                    wake: actor_wake,
                    context,
                    bus: None,
                    daemon: String::new(),
                    device: String::new(),
                    signals: Rc::new(Signals {
                        daemon: RefCell::new(String::new()),
                        device: RefCell::new(String::new()),
                        changes: RefCell::new(Changes {
                            gone: false,
                            removed: false,
                            resolved: None,
                            disconnected: false,
                        }),
                        callbacks: Cell::new(0),
                    }),
                    watches: Vec::new(),
                    attributes: Vec::new(),
                    command: Operation::IDLE,
                    stop: None,
                    connection: 0,
                    recovery: 0,
                    handled: 0,
                    stop_retry: 0,
                    owned: false,
                    gone: false,
                    removed: false,
                    resolved: false,
                    lost: false,
                    retiring: false,
                    closing: false,
                    stop_again: false,
                }
                .run();
            });
        if spawn.is_err() {
            *front = None;
            return Err(Error::Busy);
        }
        Ok(())
    }
    fn submit(session: SessionId, request: Operation) -> Result<(), Error> {
        let session = session.get();

        let c = request;
        if c.operation == 0 || c.kind() == Kind::Idle || c.value().len() > CAPACITY {
            return Err(Error::InvalidValue);
        }
        let Ok(mut guard) = FRONT.try_lock() else {
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

        let Ok(mut guard) = FRONT.try_lock() else {
            RETRY.store(session, Ordering::Release);
            poke();
            return None;
        };
        let s = guard.as_mut()?;
        if s.session != session {
            return None;
        }
        let event_out = if s.fault {
            s.fault = false;
            Some(BackendEvent::Fault)
        } else {
            s.events.pop_front()
        };
        let found = event_out.is_some();
        let retry = found && s.poisoned;
        drop(guard);
        if retry {
            poke();
        }
        event_out
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
#[path = "../../../../../tests/host/radio_shims/gatt_linux.rs"]
mod witness;
