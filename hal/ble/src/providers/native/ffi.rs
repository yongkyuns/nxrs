//! Private typed calls to the selected desktop SDK.
#![allow(unsafe_code)]

use super::Row;
use crate::{Error, Uuid};
use core::{ffi::c_void, ptr::NonNull};

unsafe extern "C" {
    fn nxrs_host_ble_scan_open(context: *mut *mut c_void) -> i32;
    fn nxrs_host_ble_scan_start(context: *mut c_void, service: *const u8) -> i32;
    fn nxrs_host_ble_scan_get(
        context: *mut c_void,
        rows: *mut Row,
        capacity: u32,
        count: *mut u32,
    ) -> i32;
    fn nxrs_host_ble_scan_stop(context: *mut c_void) -> i32;
    fn nxrs_host_ble_scan_release(context: *mut c_void);
}

pub(super) struct Bridge(Option<NonNull<c_void>>);
// SAFETY: only the private worker calls this context. SDK callbacks are confined
// to their own serial queue and retain no Rust pointers or borrowed buffers.
unsafe impl Send for Bridge {}

impl Bridge {
    pub(super) fn new() -> Self {
        Self(None)
    }
    fn context(&self) -> Result<*mut c_void, Error> {
        self.0.map(NonNull::as_ptr).ok_or(Error::Unavailable)
    }
}

impl super::Bridge for Bridge {
    fn check(&mut self) -> Result<(), Error> {
        if self.0.is_none() {
            let mut context = core::ptr::null_mut();
            // SAFETY: valid aligned output; success transfers one retained context.
            status(unsafe { nxrs_host_ble_scan_open(&mut context) })?;
            self.0 = Some(NonNull::new(context).ok_or(Error::Io)?);
        }
        Ok(())
    }
    fn start(&mut self, service: Option<Uuid>) -> Result<(), Error> {
        // SAFETY: live context; the optional 16-byte UUID is copied synchronously.
        status(unsafe {
            nxrs_host_ble_scan_start(
                self.context()?,
                service
                    .as_ref()
                    .map_or(core::ptr::null(), |uuid| uuid.0.as_ptr()),
            )
        })
    }
    fn get(&mut self, rows: &mut [Row]) -> Result<usize, Error> {
        let capacity = u32::try_from(rows.len()).map_err(|_| Error::InvalidValue)?;
        let mut count = 0;
        // SAFETY: live context and bounded writable outputs; no pointer is retained.
        status(unsafe {
            nxrs_host_ble_scan_get(self.context()?, rows.as_mut_ptr(), capacity, &mut count)
        })?;
        if count > capacity {
            return Err(Error::Io);
        }
        Ok(count as usize)
    }
    fn stop(&mut self) -> Result<(), Error> {
        // SAFETY: serial SDK barrier retires publishing callbacks before success.
        status(unsafe { nxrs_host_ble_scan_stop(self.context()?) })
    }
    fn close(&mut self) {
        if let Some(context) = self.0.take() {
            // SAFETY: called by the worker only after successful stop or idle
            // startup failure. The SDK queue detaches all delegates before release.
            unsafe { nxrs_host_ble_scan_release(context.as_ptr()) };
        }
    }
}
// Deliberately no Drop release: an unexpected worker unwind cannot prove SDK
// quiescence. The common worker retains the lease on that path as well.

fn status(value: i32) -> Result<(), Error> {
    match value {
        0 => Ok(()),
        1 => Err(Error::Unsupported),
        2 => Err(Error::Busy),
        3 => Err(Error::Unavailable),
        4 => Err(Error::PermissionDenied),
        5 => Err(Error::InvalidValue),
        6 => Err(Error::Timeout),
        _ => Err(Error::Io),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn status_mapping_and_unopened_context_do_not_touch_the_radio() {
        assert_eq!(status(0), Ok(()));
        for (index, error) in [
            Error::Unsupported,
            Error::Busy,
            Error::Unavailable,
            Error::PermissionDenied,
            Error::InvalidValue,
            Error::Timeout,
            Error::Io,
        ]
        .into_iter()
        .enumerate()
        {
            assert_eq!(status(index as i32 + 1), Err(error));
        }
        assert_eq!(status(-1), Err(Error::Io));
        assert_eq!(status(8), Err(Error::Io));
        assert_eq!(Bridge::new().context(), Err(Error::Unavailable));
    }
}
