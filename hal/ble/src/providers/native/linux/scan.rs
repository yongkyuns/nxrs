//! BlueZ scan ownership and report policy on the shared Rust scan worker.
use super::{
    gio::{self, Bus, Context, Failure, G, V},
    identity::{identity, uuid},
    Row,
};
use std::prelude::v1::*;
use std::rc::Rc;
use std::vec;
use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
    ffi::{c_char, c_void},
};
const BLUEZ: &str = "org.bluez";
const ADAPTER: &str = "org.bluez.Adapter1";
const DEVICE: &str = "org.bluez.Device1";
const PROPERTIES: &str = "org.freedesktop.DBus.Properties";
const OBJECTS: &str = "org.freedesktop.DBus.ObjectManager";
fn error(e: Failure) -> i32 {
    match e.class {
        1 => 6,
        2 => 4,
        3 => 3,
        _ => match e.name.as_str() {
            "org.bluez.Error.NotAuthorized" | "org.bluez.Error.NotPermitted" => 4,
            "org.bluez.Error.NotReady" => 3,
            "org.bluez.Error.InProgress" => 2,
            "org.bluez.Error.NotSupported" => 1,
            "org.bluez.Error.InvalidArguments" => 5,
            _ => 7,
        },
    }
}
fn required(p: &V, key: &str, kind: &str) -> Result<V, i32> {
    p.lookup(key, kind).ok_or(5)
}
struct Reports {
    scanning: bool,
    gone: bool,
    paths: VecDeque<String>,
}
struct Signals {
    daemon: String,
    reports: RefCell<Reports>,
    callbacks: Cell<usize>,
}
impl Signals {
    fn queue(&self, path: String) {
        let mut r = self.reports.borrow_mut();
        if r.scanning && path.len() < 256 && r.paths.len() < 32 && !r.paths.contains(&path) {
            r.paths.push_back(path);
        }
    }
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
    signal: *const c_char,
    parameters: G,
    data: G,
) {
    let s = unsafe { &*data.cast::<Signals>() };
    let signal = unsafe { gio::borrowed_text(signal) };
    let p = unsafe { gio::borrowed_variant(parameters) };
    if signal == "NameOwnerChanged" {
        if p.is("(sss)")
            && p.child(0).string() == s.daemon
            && !p.child(1).string().is_empty()
            && p.child(2).string().is_empty()
        {
            s.reports.borrow_mut().gone = true;
        }
        return;
    }
    if !s.reports.borrow().scanning {
        return;
    }
    let (path, values) = if signal == "InterfacesAdded" {
        if !p.is("(oa{sa{sv}})") {
            return;
        }
        let Some(values) = p.child(1).lookup(DEVICE, "a{sv}") else {
            return;
        };
        (p.child(0).string(), values)
    } else {
        if !p.is("(sa{sv}as)") || p.child(0).string() != DEVICE {
            return;
        }
        (unsafe { gio::borrowed_text(path) }, p.child(1))
    };
    if ["RSSI", "ManufacturerData", "ServiceData", "AdvertisingData"]
        .iter()
        .any(|key| values.has(key))
    {
        s.queue(path);
    }
}
struct State {
    bus: Bus,
    events: Context,
    daemon: String,
    adapter: String,
    subscriptions: Vec<u32>,
    signals: Rc<Signals>,
    service: Option<String>,
    owned: bool,
    deadline: i64,
}
impl State {
    fn deadline(&mut self, ms: i64) {
        self.deadline = gio::now() + ms * 1000;
    }
    fn call(
        &self,
        path: &str,
        interface: &str,
        method: &str,
        parameters: Option<V>,
        signature: &str,
    ) -> Result<V, i32> {
        let left = self.deadline - gio::now();
        if left <= 0 {
            return Err(6);
        }
        self.bus
            .call(
                &self.daemon,
                path,
                interface,
                method,
                parameters,
                signature,
                ((left + 999) / 1000).min(2000) as i32,
            )
            .map_err(error)
    }
    fn properties(&self, path: &str, interface: &str) -> Result<V, i32> {
        self.call(
            path,
            PROPERTIES,
            "GetAll",
            Some(V::tuple(&[V::str(interface)])),
            "(a{sv})",
        )
        .map(|v| v.child(0))
    }
    fn open() -> Result<Self, i32> {
        let bus = Bus::open().map_err(error)?;
        let daemon = bus
            .call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetNameOwner",
                Some(V::tuple(&[V::str(BLUEZ)])),
                "(s)",
                2000,
            )
            .map_err(error)?
            .child(0)
            .string();
        let signals = Rc::new(Signals {
            daemon: daemon.clone(),
            reports: RefCell::new(Reports {
                scanning: false,
                gone: false,
                paths: VecDeque::with_capacity(32),
            }),
            callbacks: Cell::new(0),
        });
        let mut s = Self {
            bus,
            events: Context::new(),
            daemon,
            adapter: String::new(),
            subscriptions: Vec::new(),
            signals,
            service: None,
            owned: false,
            deadline: 0,
        };
        s.deadline(5000);
        let objects = s
            .call("/", OBJECTS, "GetManagedObjects", None, "(a{oa{sa{sv}}})")?
            .child(0);
        for i in 0..objects.len() {
            let entry = objects.child(i);
            if entry.child(1).lookup(ADAPTER, "a{sv}").is_none() {
                continue;
            }
            if !s.adapter.is_empty() {
                return Err(2);
            }
            s.adapter = entry.child(0).string();
        }
        if s.adapter.is_empty() {
            return Err(3);
        }
        if !required(&s.properties(&s.adapter, ADAPTER)?, "Powered", "b")?.boolean() {
            return Err(3);
        }
        Ok(s)
    }
    fn detach(&mut self) {
        self.signals.reports.borrow_mut().scanning = false;
        for id in self.subscriptions.drain(..) {
            self.bus.unsubscribe(id);
        }
        while self.signals.callbacks.get() != 0 {
            self.events.iterate(true);
        }
        self.signals.reports.borrow_mut().paths.clear();
    }
    fn start(&mut self, service: Option<[u8; 16]>) -> i32 {
        if self.owned {
            return 2;
        }
        self.service = service.as_ref().map(uuid);
        self.deadline(5000);
        let filters = V::dict(vec![
            ("Transport", V::str("le")),
            ("DuplicateData", V::bool(true)),
            (
                "UUIDs",
                V::strings(&self.service.as_deref().into_iter().collect::<Vec<_>>()),
            ),
        ]);
        if let Err(e) = self.call(
            &self.adapter,
            ADAPTER,
            "SetDiscoveryFilter",
            Some(V::tuple(&[filters])),
            "()",
        ) {
            return e;
        }
        self.signals.callbacks.set(3);
        let data = Rc::as_ptr(&self.signals).cast_mut().cast();
        for (name, interface, member, path, arg) in [
            (&*self.daemon, OBJECTS, "InterfacesAdded", Some("/"), None),
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
        ] {
            self.subscriptions.push(self.bus.subscribe(
                name,
                interface,
                member,
                path,
                arg,
                signal,
                data,
                detached,
                &self.events,
            ));
        }
        self.signals.reports.borrow_mut().scanning = true;
        let reply = self.bus.start(
            &self.daemon,
            &self.adapter,
            ADAPTER,
            "StartDiscovery",
            None,
            "()",
            i32::MAX,
            &self.events,
        );
        while !reply.done() {
            self.events.iterate(false);
            gio::pause(2);
        }
        let mut status = match reply.take().unwrap() {
            Ok(_) => {
                self.owned = true;
                0
            }
            Err(e) => {
                let remote = e.remote;
                let status = error(e);
                if !remote || status == 6 {
                    self.owned = true;
                }
                status
            }
        };
        if status == 0 && gio::now() >= self.deadline {
            status = 6;
        }
        if status != 0 {
            while self.owned && self.stop() != 0 {
                gio::pause(250);
            }
            self.detach();
        }
        status
    }
    fn get(&mut self, rows: &mut [Row]) -> Result<usize, i32> {
        if !self.owned || !self.signals.reports.borrow().scanning || self.bus.closed() {
            return Err(3);
        }
        self.events.pump(64);
        if self.signals.reports.borrow().gone {
            return Err(3);
        }
        self.deadline(2000);
        let pending: Vec<_> = self
            .signals
            .reports
            .borrow()
            .paths
            .iter()
            .take(rows.len())
            .cloned()
            .collect();
        let mut count = 0;
        for path in &pending {
            let p = self.properties(path, DEVICE)?;
            if required(&p, "Adapter", "o")?.string() != self.adapter {
                continue;
            }
            if let Some(service) = &self.service {
                let matched = p.lookup("UUIDs", "as").is_some_and(|uuids| {
                    (0..uuids.len()).any(|i| uuids.child(i).string().eq_ignore_ascii_case(service))
                });
                if !matched {
                    continue;
                }
            }
            let mut row = Row {
                peer: identity(&self.daemon, path),
                rssi_dbm: i32::MIN,
                matches_filter: 1,
            };
            if p.has("RSSI") {
                let value = required(&p, "RSSI", "n")?.i16();
                if value != 127 {
                    row.rssi_dbm = value.into();
                }
            }
            rows[count] = row;
            count += 1;
        }
        self.signals
            .reports
            .borrow_mut()
            .paths
            .drain(..pending.len());
        Ok(count)
    }
    fn stop(&mut self) -> i32 {
        if self.signals.reports.borrow().gone || self.bus.closed() {
            self.owned = false;
        }
        if !self.owned {
            self.detach();
            return 0;
        }
        self.deadline(2000);
        if let Err(status) = self.call(&self.adapter, ADAPTER, "StopDiscovery", None, "()") {
            let present = self
                .bus
                .call(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "NameHasOwner",
                    Some(V::tuple(&[V::str(&self.daemon)])),
                    "(b)",
                    2000,
                )
                .map_or(true, |v| v.child(0).boolean());
            if present && !self.bus.closed() {
                return status;
            }
        }
        self.owned = false;
        self.detach();
        0
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_open(out: *mut *mut c_void) -> i32 {
    if out.is_null() {
        return 5;
    }
    unsafe { *out = std::ptr::null_mut() };
    match State::open() {
        Ok(s) => {
            unsafe { *out = Box::into_raw(Box::new(s)).cast() };
            0
        }
        Err(e) => e,
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_start(context: *mut c_void, service: *const u8) -> i32 {
    if context.is_null() {
        return 5;
    }
    let service = if service.is_null() {
        None
    } else {
        Some(unsafe { *service.cast::<[u8; 16]>() })
    };
    unsafe { &mut *context.cast::<State>() }.start(service)
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_get(
    context: *mut c_void,
    rows: *mut Row,
    capacity: u32,
    count: *mut u32,
) -> i32 {
    if context.is_null() || rows.is_null() || capacity == 0 || count.is_null() {
        return 5;
    }
    unsafe { *count = 0 };
    match unsafe { &mut *context.cast::<State>() }
        .get(unsafe { std::slice::from_raw_parts_mut(rows, capacity as usize) })
    {
        Ok(n) => {
            unsafe { *count = n as u32 };
            0
        }
        Err(e) => e,
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_stop(context: *mut c_void) -> i32 {
    if context.is_null() {
        return 5;
    }
    unsafe { &mut *context.cast::<State>() }.stop()
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_release(context: *mut c_void) {
    if context.is_null() {
        return;
    }
    let s = unsafe { &mut *context.cast::<State>() };
    while s.owned && s.stop() != 0 {
        gio::pause(250);
    }
    s.detach();
    unsafe { drop(Box::from_raw(context.cast::<State>())) }
}
#[cfg(test)]
#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_pump(context: *mut c_void) {
    unsafe { &*context.cast::<State>() }.events.pump(64);
}
#[cfg(test)]
#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_field(context: *mut c_void, field: u32) -> usize {
    let s = unsafe { &*context.cast::<State>() };
    match field {
        0 => usize::from(s.owned),
        1 => usize::from(s.signals.reports.borrow().scanning),
        2 => s.signals.callbacks.get(),
        3 => s.signals.reports.borrow().paths.len(),
        _ => 0,
    }
}

#[cfg(test)]
#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_queue(context: *mut c_void, path: *const c_char) {
    unsafe { &*context.cast::<State>() }
        .signals
        .queue(unsafe { gio::borrowed_text(path) });
}
