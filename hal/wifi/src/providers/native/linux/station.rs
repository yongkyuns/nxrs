//! NetworkManager activation and rollback policy, confined to the Wi-Fi worker.
use super::gio::{self, Bus, Context, Failure, Reply, V};
use super::Station;
use crate::{
    AccessPoint, Capabilities, CredentialStorage, Credentials, Error, LinkState, Security, Ssid,
};
use std::net::IpAddr;
use std::prelude::v1::*;
use std::rc::Rc;
use std::vec;
const NM: &str = "org.freedesktop.NetworkManager";
const ROOT: &str = "/org/freedesktop/NetworkManager";
const DEVICE: &str = "org.freedesktop.NetworkManager.Device";
const WIRELESS: &str = "org.freedesktop.NetworkManager.Device.Wireless";
const AP: &str = "org.freedesktop.NetworkManager.AccessPoint";
const ACTIVE: &str = "org.freedesktop.NetworkManager.Connection.Active";
fn error(e: Failure) -> Error {
    match e.class {
        1 => Error::Timeout,
        2 => Error::PermissionDenied,
        3 => Error::Unavailable,
        4 => Error::Unsupported,
        _ => match e.name.as_str() {
            "org.freedesktop.NetworkManager.PermissionDenied"
            | "org.freedesktop.NetworkManager.NotAuthorized" => Error::PermissionDenied,
            "org.freedesktop.NetworkManager.Device.Wireless.ScanNotAllowed" => Error::Busy,
            _ => Error::Io,
        },
    }
}
fn required(v: &V, key: &str, kind: &str) -> Result<V, Error> {
    v.lookup(key, kind).ok_or(Error::Protocol)
}
pub(crate) struct State {
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
            "org.freedesktop.DBus.Properties",
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
            return Err(Error::Unavailable);
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
                return Err(Error::Busy);
            }
            s.device = path;
        }
        if s.device.is_empty() {
            Err(Error::Unavailable)
        } else {
            Ok(s)
        }
    }
    fn scan_points(&mut self, emit: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error> {
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

        for i in 0..aps.len().min(64) {
            let p = self.properties(&aps.child(i).string(), AP)?;
            let ssid = required(&p, "Ssid", "ay")?.bytes();
            if ssid.len() > 32 {
                return Err(Error::Protocol);
            }
            let flags = required(&p, "Flags", "u")?.u32();
            let wpa = required(&p, "WpaFlags", "u")?.u32();
            let rsn = required(&p, "RsnFlags", "u")?.u32();
            let point = AccessPoint {
                ssid: if ssid.is_empty() {
                    None
                } else {
                    Some(Ssid::new(&ssid)?)
                },
                security: if rsn & 0x100 != 0 {
                    Security::Wpa2Personal
                } else if flags == 0 && wpa == 0 && rsn == 0 {
                    Security::Open
                } else {
                    Security::Other
                },
                rssi_dbm: None,
            };
            if !emit(point) {
                return Ok(false);
            }
        }
        Ok(aps.len() <= 64)
    }
    fn addressed(&self, path: &str, ipv6: bool) -> Result<bool, Error> {
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
                return Err(Error::Protocol);
            }
            let ip = text.parse::<IpAddr>().map_err(|_| Error::Protocol)?;
            if ip.is_ipv6() != ipv6 {
                return Err(Error::Protocol);
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
    fn link_state(&self) -> Result<LinkState, Error> {
        let p = self.properties(&self.device, WIRELESS)?;
        if required(&p, "ActiveAccessPoint", "o")?.string() == "/" {
            return Ok(LinkState::Disconnected);
        }
        let p = self.properties(&self.device, DEVICE)?;
        let state = required(&p, "State", "u")?.u32();
        let ipv4 = required(&p, "Ip4Config", "o")?.string();
        let ipv6 = required(&p, "Ip6Config", "o")?.string();
        if let Some(active) = &self.active {
            if required(&p, "ActiveConnection", "o")?.string() != *active {
                return Ok(LinkState::Disconnected);
            }
        }
        Ok(
            if state == 100 && (self.addressed(&ipv4, false)? || self.addressed(&ipv6, true)?) {
                LinkState::Addressed
            } else {
                LinkState::Associated
            },
        )
    }
    fn settle(&mut self) -> Result<(), Error> {
        self.events.pump(usize::MAX);
        let Some(reply) = self.activation.as_ref() else {
            return if self.uncertain {
                Err(Error::Io)
            } else {
                Ok(())
            };
        };
        let Some(result) = reply.take() else {
            return Err(Error::Busy);
        };
        self.activation = None;
        match result {
            Err(e) => {
                let remote = e.remote;
                let status = error(e);
                self.uncertain = !remote
                    || ![
                        Error::Unsupported,
                        Error::PermissionDenied,
                        Error::InvalidConfig,
                    ]
                    .contains(&status);
                return Err(if self.uncertain { Error::Io } else { status });
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
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
    fn active_state(&self) -> Result<u32, Error> {
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
            Err(Error::Protocol)
        } else {
            Ok(state)
        }
    }
    fn deactivate(&mut self) -> Result<(), Error> {
        let Some(active) = self.active.clone() else {
            return Ok(());
        };
        if self.active_state().map_err(|_| Error::Io)? == 4 {
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
            if self.active_state().map_err(|_| Error::Io)? == 4 {
                self.active = None;
                return Ok(());
            }
            gio::pause(20);
        }
    }
    fn associate(&mut self, ssid: &[u8], password: &[u8]) -> Result<(), Error> {
        if self.active.is_some() || self.activation.is_some() || self.uncertain {
            return Err(Error::Busy);
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
            return Err(Error::Busy);
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
            let pass = std::str::from_utf8(password).map_err(|_| Error::InvalidConfig)?;
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
                Err(Error::Busy) => {
                    if gio::now() >= self.deadline {
                        return Err(Error::Io);
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
                _ => break Err(Error::Io),
            };
            let p = match self.properties(&self.device, DEVICE) {
                Ok(p) => p,
                Err(e) => break Err(e),
            };
            match required(&p, "ActiveConnection", "o") {
                Ok(v) if Some(v.string()) == self.active => {}
                Ok(_) => break Err(Error::Io),
                Err(e) => break Err(e),
            }
            match self.link_state() {
                Ok(LinkState::Addressed) if value == 2 => return Ok(()),
                Ok(_) => {}
                Err(e) => break Err(e),
            }
            if gio::now() >= self.deadline {
                break Err(Error::Timeout);
            }
            gio::pause(20);
        };
        self.deadline(5000);
        self.deactivate().map_err(|_| Error::Io)?;
        outcome
    }
}

// SAFETY: the private worker is the sole owner/caller. SDK state is acquired
// on that worker and never transferred after initialization.
unsafe impl Send for State {}

impl Station for State {
    fn open(_storage: CredentialStorage) -> Result<Self, Error> {
        Self::acquire()
    }
    fn capabilities(_storage: CredentialStorage) -> Capabilities {
        Capabilities {
            scan: true,
            open_network: true,
            wpa2_personal: true,
            credential_storage: CredentialStorage::Volatile,
        }
    }
    fn scan(&mut self, emit: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error> {
        self.scan_points(emit)
    }
    fn disconnect(&mut self) -> Result<(), Error> {
        if self.active.is_none() {
            return Err(Error::Busy);
        }
        self.deadline(5000);
        self.deactivate()
    }
    fn link(&mut self) -> Result<LinkState, Error> {
        self.deadline(2000);
        self.link_state()
    }
    fn close(&mut self) -> Result<(), Error> {
        let settled = self.settle();
        if self.activation.is_some() || self.uncertain {
            return Err(settled.err().unwrap_or(Error::Io));
        }
        self.deadline(5000);
        self.deactivate()
    }

    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error> {
        let password = match credentials {
            Credentials::Open => &[][..],
            Credentials::Wpa2Personal(p) => p.as_bytes(),
        };
        self.associate(ssid.as_bytes(), password)
    }
}
