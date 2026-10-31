#![allow(unsafe_code)]

use super::{normalize_scan, read_scan_with_retry, station, Field, SCAN_READ_TIMEOUT};
use crate::worker::Driver;
use crate::{AccessPoint, Capabilities, Credentials, Error, LinkState, Ssid};
use std::boxed::Box;
use std::ffi::{c_char, c_int, CString};
use std::io;
use std::net::{Ipv4Addr, SocketAddrV4, UdpSocket};
use std::os::fd::AsRawFd;
use std::thread;
use std::time::{Duration, Instant};
use std::vec;

const SCAN_BYTES: usize = 4096;

unsafe extern "C" {
    fn wifi_station_available() -> c_int;
    fn wifi_quiescence_available() -> c_int;
    fn wifi_interface_flags_get(
        fd: c_int,
        interface: *const c_char,
        flags: *mut u32,
        up_mask: *mut u32,
    ) -> c_int;
    fn wifi_interface_flags_set(fd: c_int, interface: *const c_char, flags: u32) -> c_int;
    fn wifi_station_link(fd: c_int, interface: *const c_char, state: *mut u32) -> c_int;
    fn wifi_station_infra(fd: c_int, interface: *const c_char) -> c_int;
    fn wifi_station_wpa(fd: c_int, interface: *const c_char, secured: c_int) -> c_int;
    fn wifi_station_cipher(fd: c_int, interface: *const c_char, secured: c_int) -> c_int;
    fn wifi_station_key(
        fd: c_int,
        interface: *const c_char,
        password: *const u8,
        length: usize,
    ) -> c_int;
    fn wifi_station_ssid(
        fd: c_int,
        interface: *const c_char,
        ssid: *const u8,
        length: usize,
    ) -> c_int;
    fn wifi_station_disconnect(fd: c_int, interface: *const c_char) -> c_int;
    fn wifi_station_address(interface: *const c_char, field: u32, value: u32) -> c_int;
    fn wifi_station_mac(interface: *const c_char, mac: *mut u8) -> c_int;
    fn wifi_dhcp_open(
        interface: *const c_char,
        mac: *const u8,
        client: *mut *mut core::ffi::c_void,
    ) -> c_int;
    fn wifi_dhcp_request(client: *mut core::ffi::c_void, lease: *mut station::Lease) -> c_int;
    fn wifi_dhcp_close(client: *mut core::ffi::c_void);
    fn wifi_scan_check(fd: c_int, interface: *const c_char) -> c_int;
    fn wifi_scan_start(fd: c_int, interface: *const c_char) -> c_int;
    fn wifi_scan_read(
        fd: c_int,
        interface: *const c_char,
        buffer: *mut u8,
        capacity: usize,
        length: *mut usize,
    ) -> c_int;
    fn wifi_scan_next(
        buffer: *const u8,
        length: usize,
        offset: *mut usize,
        field: *mut Field,
    ) -> c_int;
}

pub(super) struct Native {
    socket: UdpSocket,
    interface: CString,
    owns_address: bool,
    lease_until: Option<Instant>,
    scan_buffer: Box<[u8]>,
}

fn open_interface() -> Result<(UdpSocket, CString), Error> {
    let interface = CString::new(option_env!("NXRS_WIFI_INTERFACE").unwrap_or("wlan0"))
        .map_err(|_| Error::InvalidConfig)?;
    let socket = UdpSocket::bind(SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 0)).map_err(io_error)?;
    // SAFETY: live descriptor and name; validation retains no pointer and has
    // no interface side effects. Already-down interfaces are refused.
    status(unsafe { wifi_scan_check(socket.as_raw_fd(), interface.as_ptr()) })?;
    Ok((socket, interface))
}

pub(super) struct QuiescenceInterface {
    socket: UdpSocket,
    interface: CString,
}

impl QuiescenceInterface {
    pub(super) fn open() -> Result<Self, Error> {
        // Only the qualified native driver/execution configuration is admitted.
        if unsafe { wifi_quiescence_available() } != 1 {
            return Err(Error::Unsupported);
        }
        let (socket, interface) = open_interface()?;
        Ok(Self { socket, interface })
    }

    fn flags(&self) -> Result<(u32, u32), Error> {
        let (mut flags, mut up_mask) = (0, 0);
        // SAFETY: scalar outputs and live descriptor/name; no retained pointers.
        status(unsafe {
            wifi_interface_flags_get(
                self.socket.as_raw_fd(),
                self.interface.as_ptr(),
                &mut flags,
                &mut up_mask,
            )
        })?;
        Ok((flags, up_mask))
    }
}

impl super::super::quiescence::Interface for QuiescenceInterface {
    fn set_enabled(&mut self, enabled: bool) -> Result<(), Error> {
        super::set_interface_enabled(
            enabled,
            || self.flags(),
            |desired| {
                // SAFETY: target-header ioctl translation copies the flags,
                // without retaining pointers.
                status(unsafe {
                    wifi_interface_flags_set(
                        self.socket.as_raw_fd(),
                        self.interface.as_ptr(),
                        desired,
                    )
                })
            },
        )
    }
}

impl Native {
    pub(super) fn open() -> Result<Self, Error> {
        let (socket, interface) = open_interface()?;
        Ok(Self {
            socket,
            interface,
            owns_address: false,
            lease_until: None,
            scan_buffer: vec![0; SCAN_BYTES].into_boxed_slice(),
        })
    }

    fn associated(&self) -> Result<bool, Error> {
        let mut state = 0;
        // SAFETY: live fd/name and aligned output, no retained pointer.
        status(unsafe {
            wifi_station_link(self.socket.as_raw_fd(), self.interface.as_ptr(), &mut state)
        })?;
        match state {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(Error::Protocol),
        }
    }

    fn clear_address(&mut self) -> Result<(), Error> {
        // SAFETY: valid NUL-terminated interface name for the synchronous call.
        station::clear(&mut Control(self))?;
        self.owns_address = false;
        self.lease_until = None;
        Ok(())
    }

    fn disassociate(&self) -> Result<(), Error> {
        // SAFETY: live control socket and name; generic disconnect, no pointers retained.
        status(unsafe {
            wifi_station_disconnect(self.socket.as_raw_fd(), self.interface.as_ptr())
        })?;
        let deadline = Instant::now() + Duration::from_secs(2);
        while self.associated()? {
            if Instant::now() >= deadline {
                return Err(Error::Io);
            }
            thread::sleep(Duration::from_millis(10));
        }
        Ok(())
    }
}

impl Driver for Native {
    fn capabilities(&self) -> Capabilities {
        // SAFETY: no-argument build-configuration witness.
        let station = unsafe { wifi_station_available() } == 1;
        Capabilities {
            scan: true,
            open_network: station,
            wpa2_personal: station,
            credential_storage: crate::CredentialStorage::Volatile,
        }
    }
    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error> {
        // Do not overwrite another OS client's live association.
        if self.associated()? {
            return Err(Error::Busy);
        }
        let password = match credentials {
            Credentials::Open => &[][..],
            Credentials::Wpa2Personal(value) => value.as_bytes(),
        };
        self.clear_address()?;
        // Track even a tentative DHCP OFFER so failure/Drop cannot leave it live.
        self.owns_address = true;
        let result =
            station::configure(&mut Control(self), ssid.as_bytes(), password).and_then(|()| {
                if !self.associated()? {
                    return Err(Error::Protocol);
                }
                let started = Instant::now();
                let mut mac = [0; 6];
                let mut client = core::ptr::null_mut();
                // SAFETY: synchronous target SDK calls; aligned fixed-size outputs.
                status(unsafe { wifi_station_mac(self.interface.as_ptr(), mac.as_mut_ptr()) })?;
                status(unsafe {
                    wifi_dhcp_open(self.interface.as_ptr(), mac.as_ptr(), &mut client)
                })?;
                let client = Dhcp(core::ptr::NonNull::new(client).ok_or(Error::Protocol)?);
                let mut lease = station::Lease::default();
                status(unsafe { wifi_dhcp_request(client.0.as_ptr(), &mut lease) })?;
                drop(client);
                let seconds = station::install(&mut Control(self), lease)?;
                self.lease_until = Some(
                    started
                        .checked_add(Duration::from_secs(u64::from(seconds)))
                        .ok_or(Error::Protocol)?,
                );
                if self.link()? != LinkState::Addressed {
                    return Err(Error::Unavailable);
                }
                Ok(())
            });
        if result.is_err() {
            // A recoverable completion requires BOTH native disconnect and
            // address cleanup to succeed. Otherwise fault the whole session.
            let disconnect = self.disassociate();
            let clear = self.clear_address();
            if disconnect.is_err() || clear.is_err() {
                return Err(Error::Io);
            }
        }
        result
    }
    fn disconnect(&mut self) -> Result<(), Error> {
        let disconnected = self.disassociate();
        let cleared = if self.owns_address {
            self.clear_address()
        } else {
            Ok(())
        };
        if disconnected.is_err() || cleared.is_err() {
            // Never classify a disconnect timeout as a clean, retryable slot.
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
    fn link(&mut self) -> Result<LinkState, Error> {
        let associated = self.associated()?;
        if self.owns_address
            && (!associated
                || self
                    .lease_until
                    .is_some_and(|until| Instant::now() >= until))
        {
            self.clear_address()?;
        }
        Ok(if !associated {
            LinkState::Disconnected
        } else if self.lease_until.is_some() {
            LinkState::Addressed
        } else {
            LinkState::Associated
        })
    }
    fn scan(&mut self, emit: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error> {
        // SAFETY: live borrowed std descriptor and NUL-terminated name. The C
        // helpers are synchronous, do not close fd, and retain no pointer.
        status(unsafe { wifi_scan_start(self.socket.as_raw_fd(), self.interface.as_ptr()) })?;
        let length = read_scan_with_retry(
            || {
                let mut length = 0;
                // SAFETY: buffer and aligned output are valid throughout the call.
                status(unsafe {
                    wifi_scan_read(
                        self.socket.as_raw_fd(),
                        self.interface.as_ptr(),
                        self.scan_buffer.as_mut_ptr(),
                        self.scan_buffer.len(),
                        &mut length,
                    )
                })?;
                if length > self.scan_buffer.len() {
                    return Err(Error::Protocol);
                }
                Ok(length)
            },
            SCAN_READ_TIMEOUT,
        )?;
        let buffer = &self.scan_buffer[..length];
        normalize_scan(
            length,
            |offset| {
                let mut field = Field::default();
                // SAFETY: bounded native stream and aligned outputs. The C helper
                // validates target layout and relative offsets without retaining pointers.
                match unsafe { wifi_scan_next(buffer.as_ptr(), buffer.len(), offset, &mut field) } {
                    0 => Ok(None),
                    1 => Ok(Some(field)),
                    _ => Err(Error::Protocol),
                }
            },
            emit,
        )
    }
    fn close(&mut self) -> Result<(), Error> {
        if self.owns_address {
            self.clear_address()?;
        }
        Ok(())
    }
}

impl Drop for Native {
    fn drop(&mut self) {
        // Stop is not disassociation. Relinquish only this session's IP lease;
        // retaining an address without renewal/expiry monitoring is unsafe.
        if self.owns_address {
            let _ = self.clear_address();
        }
    }
}

fn status(code: c_int) -> Result<(), Error> {
    match code {
        0 => Ok(()),
        n if n > 0 => Err(io_error(io::Error::from_raw_os_error(n))),
        _ => Err(Error::Protocol),
    }
}
fn io_error(error: io::Error) -> Error {
    match error.kind() {
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted => Error::Busy,
        io::ErrorKind::TimedOut => Error::Timeout,
        io::ErrorKind::NetworkDown | io::ErrorKind::NotFound => Error::Unavailable,
        io::ErrorKind::PermissionDenied => Error::PermissionDenied,
        io::ErrorKind::Unsupported => Error::Unsupported,
        io::ErrorKind::InvalidInput | io::ErrorKind::InvalidData => Error::InvalidConfig,
        _ => Error::Io,
    }
}

// Only this borrowed adapter crosses the target-header ABI. It retains no pointer.
struct Control<'a>(&'a Native);
impl station::Controls for Control<'_> {
    fn infra(&mut self) -> Result<(), Error> {
        status(unsafe { wifi_station_infra(self.0.socket.as_raw_fd(), self.0.interface.as_ptr()) })
    }
    fn wpa(&mut self, secured: bool) -> Result<(), Error> {
        status(unsafe {
            wifi_station_wpa(
                self.0.socket.as_raw_fd(),
                self.0.interface.as_ptr(),
                i32::from(secured),
            )
        })
    }
    fn cipher(&mut self, secured: bool) -> Result<(), Error> {
        status(unsafe {
            wifi_station_cipher(
                self.0.socket.as_raw_fd(),
                self.0.interface.as_ptr(),
                i32::from(secured),
            )
        })
    }
    fn key(&mut self, password: &[u8]) -> Result<(), Error> {
        status(unsafe {
            wifi_station_key(
                self.0.socket.as_raw_fd(),
                self.0.interface.as_ptr(),
                password.as_ptr(),
                password.len(),
            )
        })
    }
    fn ssid(&mut self, ssid: &[u8]) -> Result<(), Error> {
        status(unsafe {
            wifi_station_ssid(
                self.0.socket.as_raw_fd(),
                self.0.interface.as_ptr(),
                ssid.as_ptr(),
                ssid.len(),
            )
        })
    }
    fn address(&mut self, field: u32, value: u32) -> Result<(), Error> {
        status(unsafe { wifi_station_address(self.0.interface.as_ptr(), field, value) })
    }
}
struct Dhcp(core::ptr::NonNull<core::ffi::c_void>);
impl Drop for Dhcp {
    fn drop(&mut self) {
        unsafe { wifi_dhcp_close(self.0.as_ptr()) };
    }
}
