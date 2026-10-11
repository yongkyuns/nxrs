use super::{Error, NativeScanner, Record};
use std::ffi::c_int;
use std::io;

unsafe extern "C" {
    fn nxrs_ble_scan_open() -> c_int;
    fn nxrs_ble_scan_check(fd: c_int) -> c_int;
    fn nxrs_ble_scan_start(fd: c_int) -> c_int;
    fn nxrs_ble_scan_get(fd: c_int, records: *mut Record, capacity: u8, count: *mut u8) -> c_int;
    fn nxrs_ble_scan_stop(fd: c_int) -> c_int;
    fn nxrs_ble_scan_close(fd: c_int);
}

pub(super) struct Native {
    fd: Option<c_int>,
}

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
            self.fd = Some(fd);
        }
        // SAFETY: fd remains owned by self for the synchronous ioctl call.
        status(unsafe { nxrs_ble_scan_check(self.fd.unwrap()) })
    }
}

impl NativeScanner for Native {
    fn check(&mut self) -> Result<(), Error> {
        self.open_check()
    }

    fn start_scan(&mut self) -> Result<(), Error> {
        self.open_check()?;
        // SAFETY: live descriptor; C constructs target-native btreq_s locally.
        status(unsafe { nxrs_ble_scan_start(self.fd.unwrap()) })
    }

    fn get(&mut self, records: &mut [Record]) -> Result<usize, Error> {
        let fd = self.fd.ok_or(Error::Unavailable)?;
        let mut count = 0;
        // SAFETY: records is a writable fixed-width array and count is aligned;
        // C bounds output to capacity and copies no native structs to Rust.
        status(unsafe {
            nxrs_ble_scan_get(
                fd,
                records.as_mut_ptr(),
                u8::try_from(records.len()).map_err(|_| Error::InvalidValue)?,
                &mut count,
            )
        })?;
        if usize::from(count) > records.len() {
            return Err(Error::Io);
        }
        Ok(usize::from(count))
    }

    fn stop_scan(&mut self) -> Result<(), Error> {
        let fd = self.fd.ok_or(Error::Unavailable)?;
        // SAFETY: live descriptor; C issues synchronous SIOCBTSCANSTOP.
        status(unsafe { nxrs_ble_scan_stop(fd) })
    }

    fn close(&mut self) {
        if let Some(fd) = self.fd.take() {
            // SAFETY: this descriptor was returned by nxrs_ble_scan_open and is
            // closed exactly once after a successful radio stop or idle teardown.
            unsafe { nxrs_ble_scan_close(fd) };
        }
    }
}

impl Drop for Native {
    fn drop(&mut self) {
        self.close();
    }
}

fn status(code: c_int) -> Result<(), Error> {
    match code {
        0 => Ok(()),
        n if n > 0 => Err(io_error(io::Error::from_raw_os_error(n))),
        _ => Err(Error::Io),
    }
}

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
