use super::{
    read_scan_with_retry, Capabilities, Credentials, Error, Field, LinkState, NativeWifi, Ssid,
    SCAN_READ_TIMEOUT,
};
use std::ffi::{c_char, c_int, CString};
use std::io;
use std::net::{Ipv4Addr, SocketAddrV4, UdpSocket};
use std::os::fd::AsRawFd;
use std::thread;
use std::time::{Duration, Instant};

unsafe extern "C" {
    fn nxrs_wifi_station_available() -> c_int;
    fn nxrs_wifi_station_link(fd: c_int, interface: *const c_char, state: *mut u32) -> c_int;
    fn nxrs_wifi_station_connect(
        fd: c_int,
        interface: *const c_char,
        ssid: *const u8,
        ssid_length: usize,
        password: *const u8,
        password_length: usize,
    ) -> c_int;
    fn nxrs_wifi_station_disconnect(fd: c_int, interface: *const c_char) -> c_int;
    fn nxrs_wifi_station_clear_address(interface: *const c_char) -> c_int;
    fn nxrs_wifi_station_dhcp(interface: *const c_char, lease_seconds: *mut u32) -> c_int;
    fn nxrs_wifi_scan_check(fd: c_int, interface: *const c_char) -> c_int;
    fn nxrs_wifi_scan_start(fd: c_int, interface: *const c_char) -> c_int;
    fn nxrs_wifi_scan_read(
        fd: c_int,
        interface: *const c_char,
        buffer: *mut u8,
        capacity: usize,
        length: *mut usize,
    ) -> c_int;
    fn nxrs_wifi_scan_next(
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
}
impl Native {
    pub(super) fn open() -> Result<Self, Error> {
        let interface = CString::new(option_env!("NXRS_WIFI_INTERFACE").unwrap_or("wlan0"))
            .map_err(|_| Error::InvalidConfig)?;
        // This is an ioctl control socket, not hostname-based networking.
        let socket =
            UdpSocket::bind(SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 0)).map_err(io_error)?;
        // SAFETY: live std descriptor and NUL-terminated name; no retained
        // pointer. Reject absent/down/non-wireless bindings before Ready.
        status(unsafe { nxrs_wifi_scan_check(socket.as_raw_fd(), interface.as_ptr()) })?;
        Ok(Self {
            socket,
            interface,
            owns_address: false,
            lease_until: None,
        })
    }

    fn associated(&self) -> Result<bool, Error> {
        let mut state = 0;
        // SAFETY: live fd/name and aligned output, no retained pointer.
        status(unsafe {
            nxrs_wifi_station_link(self.socket.as_raw_fd(), self.interface.as_ptr(), &mut state)
        })?;
        match state {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(Error::Protocol),
        }
    }

    fn clear_address(&mut self) -> Result<(), Error> {
        // SAFETY: valid NUL-terminated interface name for the synchronous call.
        status(unsafe { nxrs_wifi_station_clear_address(self.interface.as_ptr()) })?;
        self.owns_address = false;
        self.lease_until = None;
        Ok(())
    }

    fn disassociate(&self) -> Result<(), Error> {
        // SAFETY: live control socket and name; generic disconnect, no pointers retained.
        status(unsafe {
            nxrs_wifi_station_disconnect(self.socket.as_raw_fd(), self.interface.as_ptr())
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

impl NativeWifi for Native {
    fn capabilities(&self) -> Capabilities {
        // SAFETY: no-argument build-configuration witness.
        let station = unsafe { nxrs_wifi_station_available() } == 1;
        Capabilities {
            scan: true,
            open_network: station,
            wpa2_personal: station,
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
        // SAFETY: borrowed bytes/name/fd valid for the entire synchronous call.
        let result = status(unsafe {
            nxrs_wifi_station_connect(
                self.socket.as_raw_fd(),
                self.interface.as_ptr(),
                ssid.as_bytes().as_ptr(),
                ssid.as_bytes().len(),
                password.as_ptr(),
                password.len(),
            )
        })
        .and_then(|()| {
            if !self.associated()? {
                return Err(Error::Protocol);
            }
            let started = Instant::now();
            let mut seconds = 0;
            // SAFETY: valid name/output; the C helper closes its DHCP client.
            status(unsafe { nxrs_wifi_station_dhcp(self.interface.as_ptr(), &mut seconds) })?;
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
    fn scan(&mut self, buffer: &mut [u8]) -> Result<usize, Error> {
        // SAFETY: live borrowed std descriptor and NUL-terminated name. The C
        // helpers are synchronous, do not close fd, and retain no pointer.
        status(unsafe { nxrs_wifi_scan_start(self.socket.as_raw_fd(), self.interface.as_ptr()) })?;
        read_scan_with_retry(
            || {
                let mut length = 0;
                // SAFETY: buffer and aligned output are valid throughout the call.
                status(unsafe {
                    nxrs_wifi_scan_read(
                        self.socket.as_raw_fd(),
                        self.interface.as_ptr(),
                        buffer.as_mut_ptr(),
                        buffer.len(),
                        &mut length,
                    )
                })?;
                if length > buffer.len() {
                    return Err(Error::Protocol);
                }
                Ok(length)
            },
            SCAN_READ_TIMEOUT,
        )
    }
    fn next(&self, buffer: &[u8], offset: &mut usize) -> Result<Option<Field>, Error> {
        let mut field = Field::default();
        // SAFETY: caller's bounded slice, aligned scalar outputs; native helper
        // validates all target-layout fields and encoded relative offsets.
        match unsafe { nxrs_wifi_scan_next(buffer.as_ptr(), buffer.len(), offset, &mut field) } {
            0 => Ok(None),
            1 => Ok(Some(field)),
            _ => Err(Error::Protocol),
        }
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
