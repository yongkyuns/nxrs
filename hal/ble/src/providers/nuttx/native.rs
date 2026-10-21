#![allow(unsafe_code)]

#[cfg(not(test))]
use super::peers;
#[cfg(not(test))]
use crate::worker::{ScanReport, Scanner};
#[cfg(not(test))]
use crate::Error;
use crate::Uuid;
#[cfg(not(test))]
use std::ffi::c_int;
#[cfg(not(test))]
use std::io;
#[cfg(not(test))]
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};

#[cfg(not(test))]
unsafe extern "C" {
    fn nxrs_ble_scan_open() -> c_int;
    fn nxrs_ble_scan_check(fd: c_int) -> c_int;
    fn nxrs_ble_scan_start(fd: c_int) -> c_int;
    fn nxrs_ble_scan_get(fd: c_int, records: *mut RawRecord, capacity: u8, count: *mut u8)
        -> c_int;
    fn nxrs_ble_scan_stop(fd: c_int) -> c_int;

}

/// Fixed width bridge layout is private to the NuttX adapter.
#[repr(C)]
#[derive(Clone, Copy)]
#[cfg(not(test))]
struct RawRecord {
    address: [u8; 6],
    address_type: u8,
    rssi: i8,
    length: u8,
    data: [u8; 64],
}

#[cfg(not(test))]
impl Default for RawRecord {
    fn default() -> Self {
        Self {
            address: [0; 6],
            address_type: 0,
            rssi: 0,
            length: 0,
            data: [0; 64],
        }
    }
}

#[cfg(not(test))]
#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub(super) struct PeerKey {
    address: [u8; 6],
    address_type: u8,
}

#[cfg(not(test))]
pub(super) struct Native {
    fd: Option<OwnedFd>,
}
#[cfg(not(test))]
impl Native {
    pub(super) fn new() -> Self {
        Self { fd: None }
    }
    fn open_check(&mut self) -> Result<(), Error> {
        if self.fd.is_none() {
            // SAFETY: the C helper returns an owned descriptor or a negative errno.
            let fd = unsafe { nxrs_ble_scan_open() };
            if fd < 0 {
                return Err(io_error(io::Error::from_raw_os_error(-fd)));
            }
            // SAFETY: successful raw Bluetooth socket creation transfers ownership.
            self.fd = Some(unsafe { OwnedFd::from_raw_fd(fd) });
        }
        // SAFETY: fd remains owned by self for this synchronous ioctl.
        status(unsafe { nxrs_ble_scan_check(self.fd.as_ref().unwrap().as_raw_fd()) })
    }
}
#[cfg(not(test))]
impl Scanner for Native {
    type Peer = PeerKey;
    fn check(&mut self) -> Result<(), Error> {
        self.open_check()
    }
    fn bind_session(&mut self, session: crate::SessionId) -> Result<(), Error> {
        let fd = self.fd.as_ref().ok_or(Error::Unavailable)?.as_raw_fd();
        peers::bind(fd, session.get())
    }
    fn remember_peer(
        &mut self,
        session: crate::SessionId,
        id: crate::PeerId,
        peer: PeerKey,
    ) -> Result<(), Error> {
        let fd = self.fd.as_ref().ok_or(Error::Unavailable)?.as_raw_fd();
        peers::remember(
            fd,
            session.get(),
            id.0,
            peers::Peer {
                address: peer.address,
                address_type: peer.address_type,
            },
        )
    }

    fn start_scan(&mut self) -> Result<(), Error> {
        self.open_check()?;
        let fd = self.fd.as_ref().unwrap().as_raw_fd();
        peers::scanning(fd, true)?;
        // Keep GATT exclusion until stop succeeds, including failed starts.
        status(unsafe { nxrs_ble_scan_start(fd) })
    }
    fn get(
        &mut self,
        service: Option<Uuid>,
        output: &mut [ScanReport<PeerKey>],
    ) -> Result<usize, Error> {
        let fd = self.fd.as_ref().ok_or(Error::Unavailable)?.as_raw_fd();
        let mut raw = [RawRecord::default(); 8];
        let mut count = 0;
        // SAFETY: raw is a writable fixed-width array, bounded by the bridge capacity.
        status(unsafe {
            nxrs_ble_scan_get(
                fd,
                raw.as_mut_ptr(),
                u8::try_from(raw.len()).map_err(|_| Error::InvalidValue)?,
                &mut count,
            )
        })?;
        let count = usize::from(count);
        if count > raw.len() || count > output.len() {
            return Err(Error::Io);
        }
        for (source, destination) in raw.iter().zip(output.iter_mut()).take(count) {
            if source.length > 64 {
                return Err(Error::Io);
            }
            let data = &source.data[..usize::from(source.length)];
            *destination = ScanReport {
                peer: PeerKey {
                    address: source.address,
                    address_type: source.address_type,
                },
                rssi_dbm: Some(i16::from(source.rssi)),
                matches_filter: service.is_none_or(|uuid| advertisement_has_service(data, uuid)),
            };
        }
        Ok(count)
    }
    fn stop_scan(&mut self) -> Result<(), Error> {
        let fd = self.fd.as_ref().ok_or(Error::Unavailable)?.as_raw_fd();
        // SAFETY: live descriptor; C issues synchronous SIOCBTSCANSTOP.
        status(unsafe { nxrs_ble_scan_stop(fd) })?;
        peers::scanning(fd, false)
    }
    fn close(&mut self) {
        if let Some(fd) = self.fd.take() {
            peers::expire(fd.as_raw_fd());
            drop(fd);
        }
    }
}

pub(super) fn advertisement_has_service(data: &[u8], requested: Uuid) -> bool {
    let mut offset = 0;
    while offset < data.len() {
        let field_len = usize::from(data[offset]);
        if field_len == 0 {
            return false;
        }
        let Some(end) = offset.checked_add(field_len + 1) else {
            return false;
        };
        if end > data.len() || field_len < 1 {
            return false;
        }
        let kind = data[offset + 1];
        let values = &data[offset + 2..end];
        let width = match kind {
            0x02 | 0x03 => 2,
            0x04 | 0x05 => 4,
            0x06 | 0x07 => 16,
            _ => {
                offset = end;
                continue;
            }
        };
        if values.len().is_multiple_of(width)
            && values
                .chunks_exact(width)
                .any(|item| list_uuid(item, width) == Some(requested))
        {
            return true;
        }
        offset = end;
    }
    false
}
fn list_uuid(value: &[u8], width: usize) -> Option<Uuid> {
    let mut canonical = [0; 16];
    match width {
        2 => {
            canonical[2..4].copy_from_slice(&[value[1], value[0]]);
            canonical[4..]
                .copy_from_slice(&[0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb]);
        }
        4 => {
            canonical[..4].copy_from_slice(&[value[3], value[2], value[1], value[0]]);
            canonical[4..]
                .copy_from_slice(&[0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb]);
        }
        16 => {
            for (dst, src) in canonical.iter_mut().zip(value.iter().rev()) {
                *dst = *src;
            }
        }
        _ => return None,
    }
    Some(Uuid(canonical))
}
#[cfg(not(test))]
fn status(code: c_int) -> Result<(), Error> {
    match code {
        0 => Ok(()),
        n if n > 0 => Err(io_error(io::Error::from_raw_os_error(n))),
        _ => Err(Error::Io),
    }
}
#[cfg(not(test))]
fn io_error(error: io::Error) -> Error {
    match error.kind() {
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted => Error::Busy,
        io::ErrorKind::NotFound | io::ErrorKind::NetworkDown => Error::Unavailable,
        io::ErrorKind::PermissionDenied => Error::PermissionDenied,
        io::ErrorKind::InvalidInput | io::ErrorKind::InvalidData => Error::InvalidValue,
        io::ErrorKind::Unsupported => Error::Unsupported,
        _ => Error::Io,
    }
}
