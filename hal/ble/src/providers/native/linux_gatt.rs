//! BlueZ GATT executor. Rust owns admission, IPC sequencing and callback retirement.
use super::{
    gio::{self, Bus, Context, Failure, Reply, G, V},
    linux_identity::{identity, uuid},
};
use crate::providers::raw_gatt::{RawCommand, RawEvent, CAPACITY};
use std::prelude::v1::*;
use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
    ffi::{c_char, c_void},
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
type Notify = unsafe extern "C" fn(*mut c_void);
struct Front {
    session: u64,
    capacity: usize,
    admitted: Option<RawCommand>,
    events: VecDeque<RawEvent>,
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
    daemon: String,
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
            && p.child(0).string() == s.daemon
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
    wake: Notify,
    release: Notify,
    arg: usize,
    context: Context,
    bus: Option<Bus>,
    daemon: String,
    device: String,
    signals: Box<Signals>,
    watches: Vec<u32>,
    attributes: Vec<Attribute>,
    command: RawCommand,
    stop: Option<Box<Reply>>,
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
        self.signals.daemon = daemon;
        self.signals.callbacks.set(3);
        let data = (&mut *self.signals as *mut Signals).cast();
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
            if identity(&self.daemon, &path) != self.command.service {
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
    fn discover(&mut self, event: &mut RawEvent) -> Result<(), i32> {
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
        let service = uuid(&self.command.service);
        let characteristic = uuid(&self.command.characteristic);
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
    fn execute(&mut self) -> RawEvent {
        let mut event = RawEvent {
            operation: self.command.operation,
            kind: self.command.kind,
            ..RawEvent::EMPTY
        };
        let result = match event.kind {
            1 => self.connect(),
            _ if !self.owned || self.gone || self.connection == 0 => Err(err(7)),
            5 => {
                self.retiring = true;
                Ok(())
            }
            2 => self.discover(&mut event),
            _ => self.value(&mut event),
        };
        if let Err(e) = result {
            event.error = e;
        }
        event
    }
    fn value(&mut self, event: &mut RawEvent) -> Result<(), i32> {
        let Some(index) = usize::from(self.command.handle).checked_sub(1) else {
            return Err(err(4));
        };
        let Some(attribute) = self.attributes.get(index) else {
            return Err(err(4));
        };
        let read = event.kind == 3;
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
            V::tuple(&[
                V::byte_array(&self.command.data[..self.command.length as usize]),
                options,
            ])
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
                    event.length = bytes.len() as u16;
                    event.data[..bytes.len()].copy_from_slice(&bytes);
                }
                Ok(())
            }
            Err(e) => {
                event.error = error(&e, &mut event.att_error);
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
                    event.kind = 9;
                }
                Ok(())
            }
        }
    }
    fn enqueue(&self, event: RawEvent, release_busy: bool) -> bool {
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
        unsafe { (self.wake)(self.arg as G) };
        room
    }
    fn loss(&mut self) {
        if self.lost
            && self.enqueue(
                RawEvent {
                    operation: self.connection,
                    kind: 8,
                    ..RawEvent::EMPTY
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
                    let event = RawEvent {
                        operation: if self.recovery != 0 {
                            self.recovery
                        } else {
                            self.command.operation
                        },
                        kind: if self.recovery != 0 { 7 } else { 5 },
                        ..RawEvent::EMPTY
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
                    unsafe { (self.wake)(self.arg as G) }
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
        unsafe { (self.release)(self.arg as G) };
    }
}
unsafe extern "C" fn tick(_: G) -> i32 {
    1
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_open(
    session: u64,
    capacity: u16,
    wake: Option<Notify>,
    release: Option<Notify>,
    arg: G,
) -> i32 {
    if session == 0 || usize::from(capacity) > CAPACITY || wake.is_none() || release.is_none() {
        return err(4);
    }
    let Ok(mut front) = FRONT.try_lock() else {
        return err(5);
    };
    if front.is_some() {
        return err(5);
    }
    let wake = wake.unwrap();
    let release = release.unwrap();
    let arg = arg as usize;
    let context = *CONTROL.get_or_init(|| unsafe { gio::g_main_context_new() } as usize);
    *front = Some(Front {
        session,
        capacity: capacity.into(),
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
                capacity: capacity.into(),
                wake,
                release,
                arg,
                context,
                bus: None,
                daemon: String::new(),
                device: String::new(),
                signals: Box::new(Signals {
                    daemon: String::new(),
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
                command: RawCommand::EMPTY,
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
        return err(17);
    }
    0
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_submit(session: u64, command: *const RawCommand) -> i32 {
    if command.is_null() {
        return err(4);
    }
    let c = unsafe { *command };
    if c.operation == 0 || !(1..=5).contains(&c.kind) || usize::from(c.length) > CAPACITY {
        return err(4);
    }
    let Ok(mut guard) = FRONT.try_lock() else {
        RETRY.store(session, Ordering::Release);
        poke();
        return err(5);
    };
    let Some(s) = guard.as_mut() else {
        return err(7);
    };
    if s.session != session || s.closing {
        return err(7);
    }
    if s.poisoned {
        return err(6);
    }
    if s.busy || s.admitted.is_some() {
        return err(5);
    }
    if usize::from(c.length) > s.capacity {
        return err(8);
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
    let Ok(mut guard) = FRONT.try_lock() else {
        RETRY.store(session, Ordering::Release);
        poke();
        return 0;
    };
    let Some(s) = guard.as_mut() else {
        return 0;
    };
    if s.session != session {
        return 0;
    }
    let event_out = if s.fault {
        s.fault = false;
        Some(RawEvent::EMPTY)
    } else {
        s.events.pop_front()
    };
    let found = event_out.is_some();
    let retry = found && s.poisoned;
    if let Some(value) = event_out {
        unsafe { event.write(value) }
    }
    drop(guard);
    if retry {
        poke();
    }
    i32::from(found)
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_gatt_recover(session: u64, operation: u64) {
    RECOVER_OP.store(operation, Ordering::Release);
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
unsafe extern "C" fn nxrs_linux_gatt_test_contend(session: u64) {
    let _guard = FRONT.lock().unwrap();
    let mut event = RawEvent::EMPTY;
    assert_eq!(unsafe { nxrs_ble_gatt_poll(session, &mut event) }, 0);
}
#[cfg(test)]
#[no_mangle]
extern "C" fn nxrs_linux_gatt_test_saturate() {
    FRONT.lock().unwrap().as_mut().unwrap().events = VecDeque::from([RawEvent::EMPTY; 4]);
}
