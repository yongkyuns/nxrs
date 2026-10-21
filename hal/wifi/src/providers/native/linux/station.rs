//! NetworkManager activation and rollback policy, confined to the Wi-Fi worker.
use super::{
    gio::{self, Bus, Context, Failure, Reply, V},
    Row,
};
use std::prelude::v1::*;
use std::rc::Rc;
use std::vec;
use std::{ffi::c_void, net::IpAddr};
const NM: &str = "org.freedesktop.NetworkManager";
const ROOT: &str = "/org/freedesktop/NetworkManager";
const DEVICE: &str = "org.freedesktop.NetworkManager.Device";
const WIRELESS: &str = "org.freedesktop.NetworkManager.Device.Wireless";
const AP: &str = "org.freedesktop.NetworkManager.AccessPoint";
const ACTIVE: &str = "org.freedesktop.NetworkManager.Connection.Active";
fn error(e: Failure) -> i32 {
    match e.class {
        1 => 7,
        2 => 4,
        3 => 3,
        4 => 1,
        _ => match e.name.as_str() {
            "org.freedesktop.NetworkManager.PermissionDenied"
            | "org.freedesktop.NetworkManager.NotAuthorized" => 4,
            "org.freedesktop.NetworkManager.Device.Wireless.ScanNotAllowed" => 2,
            _ => 9,
        },
    }
}
fn required(v: &V, key: &str, kind: &str) -> Result<V, i32> {
    v.lookup(key, kind).ok_or(8)
}
struct State {
    bus: Bus,
    events: Context,
    daemon: String,
    device: String,
    active: Option<String>,
    activation: Option<Rc<Reply>>,
    uncertain: bool,
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
            return Err(7);
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
            "org.freedesktop.DBus.Properties",
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
                Some(V::tuple(&[V::str(NM)])),
                "(s)",
                2000,
            )
            .map_err(error)?
            .child(0)
            .string();
        let mut s = Self {
            bus,
            events: Context::new(),
            daemon,
            device: String::new(),
            active: None,
            activation: None,
            uncertain: false,
            deadline: 0,
        };
        s.deadline(5000);
        let p = s.properties(ROOT, NM)?;
        if !required(&p, "WirelessEnabled", "b")?.boolean()
            || !required(&p, "WirelessHardwareEnabled", "b")?.boolean()
        {
            return Err(3);
        }
        let devices = s.call(ROOT, NM, "GetDevices", None, "(ao)")?.child(0);
        for i in 0..devices.len() {
            let path = devices.child(i).string();
            let p = s.properties(&path, DEVICE)?;
            let kind = required(&p, "DeviceType", "u")?.u32();
            let managed = required(&p, "Managed", "b")?.boolean();
            if kind != 2 || !managed {
                continue;
            }
            if !s.device.is_empty() {
                return Err(2);
            }
            s.device = path;
        }
        if s.device.is_empty() {
            Err(3)
        } else {
            Ok(s)
        }
    }
    fn scan(&mut self, rows: &mut [Row]) -> Result<(u32, u32), i32> {
        self.deadline(10000);
        let before = required(&self.properties(&self.device, WIRELESS)?, "LastScan", "x")?.i64();
        self.call(
            &self.device,
            WIRELESS,
            "RequestScan",
            Some(V::tuple(&[V::dict(vec![])])),
            "()",
        )?;
        loop {
            let after = required(&self.properties(&self.device, WIRELESS)?, "LastScan", "x")?.i64();
            if after > before {
                break;
            }
            gio::pause(20);
        }
        let aps = self
            .call(&self.device, WIRELESS, "GetAllAccessPoints", None, "(ao)")?
            .child(0);
        let count = aps.len().min(rows.len());
        for (i, row) in rows.iter_mut().take(count).enumerate() {
            let p = self.properties(&aps.child(i).string(), AP)?;
            let ssid = required(&p, "Ssid", "ay")?.bytes();
            if ssid.len() > 32 {
                return Err(8);
            }
            let flags = required(&p, "Flags", "u")?.u32();
            let wpa = required(&p, "WpaFlags", "u")?.u32();
            let rsn = required(&p, "RsnFlags", "u")?.u32();
            *row = Row {
                ssid_length: ssid.len() as u32,
                security: if rsn & 0x100 != 0 {
                    1
                } else if flags == 0 && wpa == 0 && rsn == 0 {
                    0
                } else {
                    2
                },
                rssi_dbm: i32::MIN,
                ssid: [0; 32],
            };
            row.ssid[..ssid.len()].copy_from_slice(&ssid);
        }
        Ok((count as u32, u32::from(aps.len() > rows.len())))
    }
    fn addressed(&self, path: &str, ipv6: bool) -> Result<bool, i32> {
        if path == "/" {
            return Ok(false);
        }
        let interface = if ipv6 {
            "org.freedesktop.NetworkManager.IP6Config"
        } else {
            "org.freedesktop.NetworkManager.IP4Config"
        };
        let p = self.properties(path, interface)?;
        let addresses = required(&p, "AddressData", "aa{sv}")?;
        for i in 0..addresses.len() {
            let row = addresses.child(i);
            let text = required(&row, "address", "s")?.string();
            let prefix = required(&row, "prefix", "u")?.u32();
            if prefix > if ipv6 { 128 } else { 32 } {
                return Err(8);
            }
            let ip = text.parse::<IpAddr>().map_err(|_| 8)?;
            if ip.is_ipv6() != ipv6 {
                return Err(8);
            }
            let usable = match ip {
                IpAddr::V4(v) => {
                    !v.is_unspecified()
                        && !v.is_loopback()
                        && !v.is_link_local()
                        && !v.is_multicast()
                }
                IpAddr::V6(v) => {
                    !v.is_unspecified()
                        && !v.is_loopback()
                        && !v.is_unicast_link_local()
                        && !v.is_multicast()
                }
            };
            if usable {
                return Ok(true);
            }
        }
        Ok(false)
    }
    fn link(&self) -> Result<u32, i32> {
        let p = self.properties(&self.device, WIRELESS)?;
        if required(&p, "ActiveAccessPoint", "o")?.string() == "/" {
            return Ok(1);
        }
        let p = self.properties(&self.device, DEVICE)?;
        let state = required(&p, "State", "u")?.u32();
        let ipv4 = required(&p, "Ip4Config", "o")?.string();
        let ipv6 = required(&p, "Ip6Config", "o")?.string();
        if let Some(active) = &self.active {
            if required(&p, "ActiveConnection", "o")?.string() != *active {
                return Ok(1);
            }
        }
        Ok(
            if state == 100 && (self.addressed(&ipv4, false)? || self.addressed(&ipv6, true)?) {
                3
            } else {
                2
            },
        )
    }
    fn settle(&mut self) -> Result<(), i32> {
        self.events.pump(usize::MAX);
        let Some(reply) = self.activation.as_ref() else {
            return if self.uncertain { Err(9) } else { Ok(()) };
        };
        let Some(result) = reply.take() else {
            return Err(2);
        };
        self.activation = None;
        match result {
            Err(e) => {
                let remote = e.remote;
                let status = error(e);
                self.uncertain = !remote || ![1, 4, 6].contains(&status);
                return Err(if self.uncertain { 9 } else { status });
            }
            Ok(v) => {
                let profile = v.child(0).string();
                let active = v.child(1).string();
                if profile == "/" || active == "/" {
                    self.uncertain = true;
                } else {
                    self.active = Some(active);
                }
            }
        }
        if self.uncertain {
            Err(9)
        } else {
            Ok(())
        }
    }
    fn active_state(&self) -> Result<u32, i32> {
        let Some(active) = &self.active else {
            return Ok(4);
        };
        let p = self.properties(ROOT, NM)?;
        let connections = required(&p, "ActiveConnections", "ao")?;
        if !(0..connections.len()).any(|i| connections.child(i).string() == *active) {
            return Ok(4);
        }
        let p = self.properties(active, ACTIVE)?;
        let state = required(&p, "State", "u")?.u32();
        if state > 4 {
            Err(8)
        } else {
            Ok(state)
        }
    }
    fn deactivate(&mut self) -> Result<(), i32> {
        let Some(active) = self.active.clone() else {
            return Ok(());
        };
        if self.active_state().map_err(|_| 9)? == 4 {
            self.active = None;
            return Ok(());
        }
        let _ = self.call(
            ROOT,
            NM,
            "DeactivateConnection",
            Some(V::tuple(&[V::path(&active)])),
            "()",
        );
        loop {
            if self.active_state().map_err(|_| 9)? == 4 {
                self.active = None;
                return Ok(());
            }
            gio::pause(20);
        }
    }
    fn connect(&mut self, ssid: &[u8], password: &[u8]) -> Result<(), i32> {
        if self.active.is_some() || self.activation.is_some() || self.uncertain {
            return Err(2);
        }
        self.deadline(20000);
        if required(
            &self.properties(&self.device, DEVICE)?,
            "ActiveConnection",
            "o",
        )?
        .string()
            != "/"
        {
            return Err(2);
        }
        let mut settings = vec![
            (
                "connection",
                V::dict(vec![
                    ("id", V::str("nxrs-wifi")),
                    ("uuid", V::str(&gio::random_uuid())),
                    ("type", V::str("802-11-wireless")),
                    ("autoconnect", V::bool(false)),
                ]),
            ),
            (
                "802-11-wireless",
                V::dict(vec![
                    ("ssid", V::byte_array(ssid)),
                    ("mode", V::str("infrastructure")),
                ]),
            ),
        ];
        if !password.is_empty() {
            let pass = std::str::from_utf8(password).map_err(|_| 6)?;
            settings.push((
                "802-11-wireless-security",
                V::dict(vec![
                    ("key-mgmt", V::str("wpa-psk")),
                    ("proto", V::strings(&["rsn"])),
                    ("psk", V::str(pass)),
                    ("psk-flags", V::uint(2)),
                ]),
            ));
        }
        settings.push(("ipv4", V::dict(vec![("method", V::str("auto"))])));
        settings.push(("ipv6", V::dict(vec![("method", V::str("auto"))])));
        let settings = V::array(
            "{sa{sv}}",
            &settings
                .into_iter()
                .map(|(k, v)| V::entry(V::str(k), v))
                .collect::<Vec<_>>(),
        );
        let options = V::dict(vec![
            ("persist", V::str("volatile")),
            ("bind-activation", V::str("dbus-client")),
        ]);
        self.activation = Some(self.bus.start(
            &self.daemon,
            ROOT,
            NM,
            "AddAndActivateConnection2",
            Some(V::tuple(&[
                settings,
                V::path(&self.device),
                V::path("/"),
                options,
            ])),
            "(ooa{sv})",
            i32::MAX,
            &self.events,
        ));
        loop {
            match self.settle() {
                Err(2) => {
                    if gio::now() >= self.deadline {
                        return Err(9);
                    }
                    gio::pause(20);
                }
                other => {
                    other?;
                    break;
                }
            }
        }
        let outcome = loop {
            let value = match self.active_state() {
                Ok(value) if value != 4 => value,
                _ => break Err(9),
            };
            let p = match self.properties(&self.device, DEVICE) {
                Ok(p) => p,
                Err(e) => break Err(e),
            };
            match required(&p, "ActiveConnection", "o") {
                Ok(v) if Some(v.string()) == self.active => {}
                Ok(_) => break Err(9),
                Err(e) => break Err(e),
            }
            match self.link() {
                Ok(3) if value == 2 => return Ok(()),
                Ok(_) => {}
                Err(e) => break Err(e),
            }
            if gio::now() >= self.deadline {
                break Err(7);
            }
            gio::pause(20);
        };
        self.deadline(5000);
        self.deactivate().map_err(|_| 9)?;
        outcome
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_open(out: *mut *mut c_void, storage: u32) -> i32 {
    if out.is_null() {
        return 6;
    }
    unsafe { *out = std::ptr::null_mut() };
    if storage > 1 {
        return 6;
    }
    match State::open() {
        Ok(s) => {
            unsafe { *out = Box::into_raw(Box::new(s)).cast() };
            0
        }
        Err(e) => e,
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_scan(
    context: *mut c_void,
    rows: *mut Row,
    capacity: u32,
    count: *mut u32,
    truncated: *mut u32,
) -> i32 {
    if context.is_null()
        || rows.is_null()
        || capacity == 0
        || count.is_null()
        || truncated.is_null()
    {
        return 6;
    }
    unsafe {
        *count = 0;
        *truncated = 0;
    }
    let s = unsafe { &mut *context.cast::<State>() };
    match s.scan(unsafe { std::slice::from_raw_parts_mut(rows, capacity as usize) }) {
        Ok((n, t)) => {
            unsafe {
                *count = n;
                *truncated = t
            };
            0
        }
        Err(e) => e,
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_link(context: *mut c_void, link: *mut u32) -> i32 {
    if context.is_null() || link.is_null() {
        return 6;
    }
    let s = unsafe { &mut *context.cast::<State>() };
    s.deadline(2000);
    match s.link() {
        Ok(value) => {
            unsafe { *link = value };
            0
        }
        Err(e) => e,
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_connect(
    context: *mut c_void,
    ssid: *const u8,
    ssid_len: usize,
    password: *const u8,
    password_len: usize,
) -> i32 {
    if context.is_null()
        || ssid.is_null()
        || ssid_len == 0
        || ssid_len > 32
        || (password_len != 0 && (password.is_null() || !(8..=63).contains(&password_len)))
    {
        return 6;
    }
    let ssid = unsafe { std::slice::from_raw_parts(ssid, ssid_len) };
    let password = if password_len == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(password, password_len) }
    };
    if password.iter().any(|v| !(0x20..=0x7e).contains(v)) {
        return 6;
    }
    let s = unsafe { &mut *context.cast::<State>() };
    match s.connect(ssid, password) {
        Ok(()) => 0,
        Err(e) => e,
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_disconnect(context: *mut c_void) -> i32 {
    if context.is_null() {
        return 6;
    }
    let s = unsafe { &mut *context.cast::<State>() };
    if s.active.is_none() {
        return 2;
    }
    s.deadline(5000);
    match s.deactivate() {
        Ok(()) => 0,
        Err(e) => e,
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_wifi_release(context: *mut c_void) -> i32 {
    if context.is_null() {
        return 0;
    }
    let s = unsafe { &mut *context.cast::<State>() };
    let settled = s.settle();
    if s.activation.is_some() || s.uncertain {
        return settled.err().unwrap_or(9);
    }
    s.deadline(5000);
    if let Err(e) = s.deactivate() {
        return e;
    }
    unsafe { drop(Box::from_raw(context.cast::<State>())) };
    0
}
