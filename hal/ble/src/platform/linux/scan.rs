//! BlueZ scan ownership and report policy on the shared Rust scan worker.
use super::ScanBackend;
use super::{
    gio::{self, Bus, Context, Failure, G, V},
    identity::{identity, uuid},
    Report,
};
use crate::{Error, Uuid};
use std::prelude::v1::*;
use std::rc::Rc;
use std::vec;
use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
    ffi::c_char,
};
const BLUEZ: &str = "org.bluez";
const ADAPTER: &str = "org.bluez.Adapter1";
const DEVICE: &str = "org.bluez.Device1";
const PROPERTIES: &str = "org.freedesktop.DBus.Properties";
const OBJECTS: &str = "org.freedesktop.DBus.ObjectManager";
fn error(e: Failure) -> Error {
    match e.class {
        1 => Error::Timeout,
        2 => Error::PermissionDenied,
        3 => Error::Unavailable,
        _ => match e.name.as_str() {
            "org.bluez.Error.NotAuthorized" | "org.bluez.Error.NotPermitted" => {
                Error::PermissionDenied
            }
            "org.bluez.Error.NotReady" => Error::Unavailable,
            "org.bluez.Error.InProgress" => Error::Busy,
            "org.bluez.Error.NotSupported" => Error::Unsupported,
            "org.bluez.Error.InvalidArguments" => Error::InvalidValue,
            _ => Error::Io,
        },
    }
}
fn required(p: &V, key: &str, kind: &str) -> Result<V, Error> {
    p.lookup(key, kind).ok_or(Error::InvalidValue)
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
pub(crate) struct State {
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
    ) -> Result<V, Error> {
        let left = self.deadline - gio::now();
        if left <= 0 {
            return Err(Error::Timeout);
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
    fn properties(&self, path: &str, interface: &str) -> Result<V, Error> {
        self.call(
            path,
            PROPERTIES,
            "GetAll",
            Some(V::tuple(&[V::str(interface)])),
            "(a{sv})",
        )
        .map(|v| v.child(0))
    }
    fn acquire() -> Result<Self, Error> {
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
                return Err(Error::Busy);
            }
            s.adapter = entry.child(0).string();
        }
        if s.adapter.is_empty() {
            return Err(Error::Unavailable);
        }
        if !required(&s.properties(&s.adapter, ADAPTER)?, "Powered", "b")?.boolean() {
            return Err(Error::Unavailable);
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
    fn begin(&mut self, service: Option<[u8; 16]>) -> Result<(), Error> {
        if self.owned {
            return Err(Error::Busy);
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
        self.call(
            &self.adapter,
            ADAPTER,
            "SetDiscoveryFilter",
            Some(V::tuple(&[filters])),
            "()",
        )?;
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
                Ok(())
            }
            Err(e) => {
                let remote = e.remote;
                let status = error(e);
                if !remote || status == Error::Timeout {
                    self.owned = true;
                }
                Err(status)
            }
        };
        if status.is_ok() && gio::now() >= self.deadline {
            status = Err(Error::Timeout);
        }
        if status.is_err() {
            while self.owned && self.end().is_err() {
                gio::pause(250);
            }
            self.detach();
        }
        status
    }
    fn reports(&mut self, rows: &mut [Report]) -> Result<usize, Error> {
        if !self.owned || !self.signals.reports.borrow().scanning || self.bus.closed() {
            return Err(Error::Unavailable);
        }
        self.events.pump(64);
        if self.signals.reports.borrow().gone {
            return Err(Error::Unavailable);
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
            let mut row = Report {
                peer: identity(&self.daemon, path),
                rssi_dbm: None,
                matches_filter: true,
            };
            if p.has("RSSI") {
                let value = required(&p, "RSSI", "n")?.i16();
                if value != 127 {
                    row.rssi_dbm = Some(value);
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
    fn end(&mut self) -> Result<(), Error> {
        if self.signals.reports.borrow().gone || self.bus.closed() {
            self.owned = false;
        }
        if !self.owned {
            self.detach();
            return Ok(());
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
                return Err(status);
            }
        }
        self.owned = false;
        self.detach();
        Ok(())
    }
}

// SAFETY: acquired and used exclusively by the private scan worker; callback
// storage remains retained until stop and the SDK detach barrier complete.
unsafe impl Send for State {}
impl ScanBackend for State {
    fn open() -> Result<Self, Error> {
        Self::acquire()
    }
    fn start(&mut self, service: Option<Uuid>) -> Result<(), Error> {
        self.begin(service.map(|v| v.0))
    }
    fn get(&mut self, rows: &mut [Report]) -> Result<usize, Error> {
        self.reports(rows)
    }
    fn stop(&mut self) -> Result<(), Error> {
        self.end()
    }
    fn close(&mut self) {
        while self.owned && self.end().is_err() {
            gio::pause(250);
        }
        self.detach();
    }
}

#[cfg(all(test, native_witness))]
#[path = "../../../../../tests/host/radio_shims/ble_linux_scan.rs"]
mod witness;
