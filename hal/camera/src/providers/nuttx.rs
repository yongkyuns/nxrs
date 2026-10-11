//! NuttX read-device camera. The scalar C bridge uses configured target headers.
#![deny(unsafe_op_in_unsafe_fn)]
#[cfg(not(any(target_os = "nuttx", target_os = "none")))]
compile_error!("NuttX camera requires NuttX or an explicit core-only target fixture");
use crate::{Camera, Capture, DeviceError, Format, PixelFormat};
use core::ffi::{c_char, c_int, CStr};
use nuttx_support::{error, OwnedFd};
extern "C" {
    fn rc_nx_camera_open(
        path: *const c_char,
        width: *mut u16,
        height: *mut u16,
        pixels: *mut u8,
    ) -> c_int;
    fn rc_nx_read(fd: c_int, bytes: *mut u8, capacity: usize) -> c_int;
}

/// Read-device adapter with explicit format-query ioctl. This is NOT a V4L2
/// adapter. A hardware driver must implement the bridge.h read-device contract.
/// A close error is terminal: the descriptor is consumed, further reads fail,
/// and stop keeps reporting the error without issuing another close syscall.
pub struct DeviceCamera<'a> {
    path: &'a CStr,
    fd: Option<OwnedFd>,
    format: Option<Format>,
}

impl<'a> DeviceCamera<'a> {
    pub fn new(path: &'a CStr) -> Self {
        Self {
            path,
            fd: None,
            format: None,
        }
    }
}

impl Camera for DeviceCamera<'_> {
    fn start(&mut self, requested: Format) -> Result<Format, DeviceError> {
        if self.fd.is_some() {
            return Err(DeviceError::Busy);
        }
        let (mut width, mut height, mut pixels) = (0, 0, 0);
        // SAFETY: path is nul-terminated, outputs are valid for this call.
        let fd = unsafe {
            OwnedFd::from_raw(rc_nx_camera_open(
                self.path.as_ptr(),
                &mut width,
                &mut height,
                &mut pixels,
            ))
        }?;
        let pixels = match pixels {
            0 => PixelFormat::Gray8,
            1 => PixelFormat::Rgb565,
            2 => PixelFormat::Jpeg,
            _ => return Err(DeviceError::InvalidData),
        };
        let actual = Format {
            width,
            height,
            pixels,
        };
        if actual != requested {
            return Err(DeviceError::Unsupported);
        }
        self.format = Some(actual);
        self.fd = Some(fd);
        Ok(actual)
    }

    fn poll_frame(
        &mut self,
        now_ms: u64,
        destination: &mut [u8],
    ) -> Result<Option<Capture>, DeviceError> {
        let fd = self.fd.as_ref().ok_or(DeviceError::Io)?.raw()?;
        // SAFETY: exclusive buffer and owned open descriptor; bridge bounds read.
        let len = unsafe { rc_nx_read(fd, destination.as_mut_ptr(), destination.len()) };
        if len == 0 {
            return Ok(None);
        }
        if len < 0 {
            return Err(error(len));
        }
        if len as usize > destination.len() {
            return Err(DeviceError::InvalidData);
        }
        // For this read-device contract, time is owner observation time, NOT a
        // sensor exposure timestamp. Frame format comes from the actual device.
        Ok(Some(Capture {
            format: self.format.ok_or(DeviceError::Io)?,
            len: len as usize,
            timestamp_ms: now_ms,
        }))
    }

    fn stop(&mut self) -> Result<(), DeviceError> {
        if let Some(fd) = self.fd.as_mut() {
            fd.close()?;
        }
        self.fd = None;
        self.format = None;
        Ok(())
    }
}
