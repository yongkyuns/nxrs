//! NuttX's standard capture queue. Driver buffers never borrow app memory.
#![allow(unsafe_code)]

use crate::{Camera, Capture, DeviceError, Format, PixelFormat};
use std::alloc::{alloc_zeroed, dealloc, Layout};
use std::fs::{File, OpenOptions};
use std::os::fd::{AsRawFd, IntoRawFd};
use std::os::unix::fs::OpenOptionsExt;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicBool, Ordering};

unsafe extern "C" {
    fn nxrs_camera_open_flags() -> i32;
    fn nxrs_camera_format(
        fd: i32,
        w: u16,
        h: u16,
        pixels: u8,
        actual_w: *mut u32,
        actual_h: *mut u32,
        actual_pixels: *mut u8,
    ) -> i32;
    fn nxrs_camera_buffers(fd: i32, count: u32) -> i32;
    fn nxrs_camera_queue(fd: i32, data: *mut u8, length: usize) -> i32;
    fn nxrs_camera_dequeue(
        fd: i32,
        address: *mut usize,
        capacity: *mut u32,
        bytes: *mut u32,
        flags: *mut u32,
    ) -> i32;
    fn nxrs_camera_stream(fd: i32, on: i32) -> i32;
    fn rc_nx_close(fd: i32) -> i32;
}

static ACQUIRED: AtomicBool = AtomicBool::new(false);

#[cfg(not(test))]
const DEVICE_PATH: &str = "/dev/video0";
// Host tests exercise the production ownership code without camera hardware.
#[cfg(test)]
const DEVICE_PATH: &str = "/dev/null";

fn status(code: i32) -> Result<(), DeviceError> {
    match code {
        0 => Ok(()),
        -1 => Err(DeviceError::Unsupported),
        -2 => Err(DeviceError::Busy),
        -3 => Err(DeviceError::Timeout),
        -4 => Err(DeviceError::Full),
        -5 => Err(DeviceError::InvalidData),
        _ => Err(DeviceError::Io),
    }
}

struct Buffer {
    pointer: NonNull<u8>,
    layout: Layout,
}

impl Buffer {
    fn new(bytes: usize) -> Result<Self, DeviceError> {
        let layout = Layout::from_size_align(bytes, 64).map_err(|_| DeviceError::Full)?;
        // SAFETY: nonzero checked frame size, matching layout retained for Drop.
        let pointer = NonNull::new(unsafe { alloc_zeroed(layout) }).ok_or(DeviceError::Full)?;
        Ok(Self { pointer, layout })
    }
}

impl Drop for Buffer {
    fn drop(&mut self) {
        // SAFETY: only released after capture is quiescent; exact allocation layout.
        unsafe { dealloc(self.pointer.as_ptr(), self.layout) };
    }
}

pub struct LiveCamera {
    file: Option<File>,
    buffer: Option<Buffer>,
    format: Option<Format>,
    swap: bool,
    queued: bool,
    ready: Option<usize>,
    stream_may_run: bool,
    next_poll_ms: Option<u64>,
    close_error: Option<DeviceError>,
    last_poll_ms: Option<u64>,
    fault: Option<DeviceError>,
}

impl LiveCamera {
    pub fn acquire() -> Result<Self, DeviceError> {
        ACQUIRED
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| DeviceError::Busy)?;
        Ok(Self {
            file: None,
            buffer: None,
            format: None,
            swap: false,
            queued: false,
            ready: None,
            stream_may_run: false,
            next_poll_ms: None,
            close_error: None,
            last_poll_ms: None,
            fault: None,
        })
    }

    fn configure(&mut self, requested: Format) -> Result<(), DeviceError> {
        let pixels = match requested.pixels {
            PixelFormat::Rgb565 => 1,
            PixelFormat::Jpeg => 2,
            PixelFormat::Gray8 => return Err(DeviceError::Unsupported),
        };
        if requested.width == 0 || requested.height == 0 {
            return Err(DeviceError::InvalidData);
        }
        // V4L2's NuttX capture upper half requires this maximum extent for JPEG too.
        let capacity = usize::from(requested.width)
            .checked_mul(usize::from(requested.height))
            .and_then(|value| value.checked_mul(2))
            .ok_or(DeviceError::Full)?;
        // SAFETY: obtains a target-defined open flag, no retained pointers.
        let flags = unsafe { nxrs_camera_open_flags() };
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(flags)
            .open(DEVICE_PATH)
            .map_err(|_| DeviceError::Io)?;
        self.file = Some(file);
        let fd = self.file.as_ref().unwrap().as_raw_fd();
        let (mut width, mut height, mut actual) = (0, 0, 0);
        // SAFETY: scalar outputs live through the synchronous target-header shim.
        let mut code = unsafe {
            nxrs_camera_format(
                fd,
                requested.width,
                requested.height,
                pixels,
                &mut width,
                &mut height,
                &mut actual,
            )
        };
        if code == -5 && pixels == 1 {
            // Normalize a sensor's big-endian RGB565 in this provider, not the app.
            code = unsafe {
                nxrs_camera_format(
                    fd,
                    requested.width,
                    requested.height,
                    3,
                    &mut width,
                    &mut height,
                    &mut actual,
                )
            };
        }
        if code == -5 {
            return Err(DeviceError::Unsupported);
        }
        status(code)?;
        if width != u32::from(requested.width)
            || height != u32::from(requested.height)
            || (actual != pixels && !(pixels == 1 && actual == 3))
        {
            return Err(DeviceError::Unsupported);
        }
        self.swap = actual == 3;
        self.buffer = Some(Buffer::new(capacity)?);
        // SAFETY: owned descriptor, one FIFO container; buffer stays at a stable address.
        status(unsafe { nxrs_camera_buffers(fd, 1) })?;
        self.queue()?;
        self.stream_may_run = true;
        status(unsafe { nxrs_camera_stream(fd, 1) })?;
        self.format = Some(requested);
        self.next_poll_ms = Some(0); // An active polling provider needs an initial poll.
        Ok(())
    }

    fn queue(&mut self) -> Result<(), DeviceError> {
        let fd = self.file.as_ref().ok_or(DeviceError::Io)?.as_raw_fd();
        let buffer = self.buffer.as_ref().ok_or(DeviceError::Io)?;
        // SAFETY: allocation is retained until stream-off; no Rust slice aliases DMA.
        status(unsafe { nxrs_camera_queue(fd, buffer.pointer.as_ptr(), buffer.layout.size()) })?;
        self.queued = true;
        Ok(())
    }
}

impl Camera for LiveCamera {
    fn start(&mut self, requested: Format) -> Result<Format, DeviceError> {
        if self.file.is_some() {
            return Err(DeviceError::Busy);
        }
        if let Some(error) = self.close_error {
            return Err(error);
        }
        if let Err(error) = self.configure(requested) {
            self.stop()?;
            return Err(error);
        }
        Ok(requested)
    }

    fn poll_frame(
        &mut self,
        now_ms: u64,
        destination: &mut [u8],
    ) -> Result<Option<Capture>, DeviceError> {
        let format = self.format.ok_or(DeviceError::Io)?;
        if let Some(error) = self.fault {
            return Err(error);
        }
        if self.last_poll_ms.is_some_and(|previous| now_ms < previous) {
            return Err(DeviceError::InvalidData);
        }
        self.last_poll_ms = Some(now_ms);
        let fd = self.file.as_ref().ok_or(DeviceError::Io)?.as_raw_fd();
        if !self.queued && self.ready.is_none() {
            self.queue()?;
        }
        if self.ready.is_none() {
            let (mut address, mut capacity, mut bytes, mut flags) = (0, 0, 0, 0);
            // SAFETY: fixed-width outputs; O_NONBLOCK prevents waiting for a frame.
            let code = unsafe {
                nxrs_camera_dequeue(fd, &mut address, &mut capacity, &mut bytes, &mut flags)
            };
            if code == 1 {
                self.next_poll_ms = Some(now_ms.saturating_add(5));
                return Ok(None);
            }
            if let Err(error) = status(code) {
                self.fault = Some(error);
                self.next_poll_ms = None;
                return Err(error);
            }
            self.queued = false;
            let buffer = self.buffer.as_ref().ok_or(DeviceError::Io)?;
            if flags != 0
                || address != buffer.pointer.as_ptr() as usize
                || capacity as usize != buffer.layout.size()
                || bytes as usize > buffer.layout.size()
                || !format.accepts_len(bytes as usize)
            {
                self.fault = Some(DeviceError::InvalidData);
                self.next_poll_ms = None;
                return Err(DeviceError::InvalidData);
            }
            self.ready = Some(bytes as usize);
        }
        let bytes = self.ready.unwrap();
        if destination.len() < bytes {
            return Err(DeviceError::Full);
        }
        let buffer = self.buffer.as_ref().ok_or(DeviceError::Io)?;
        // SAFETY: successful dequeue ended the driver's single-buffer FIFO capture.
        // The caller's destination is disjoint from the provider-owned DMA allocation.
        unsafe {
            std::ptr::copy_nonoverlapping(buffer.pointer.as_ptr(), destination.as_mut_ptr(), bytes)
        };
        if self.swap {
            for word in destination[..bytes].chunks_exact_mut(2) {
                word.swap(0, 1);
            }
        }
        self.ready = None;
        self.next_poll_ms = Some(now_ms);
        // Observation time, not a claimed sensor exposure timestamp.
        Ok(Some(Capture {
            format,
            len: bytes,
            timestamp_ms: now_ms,
        }))
    }

    fn next_poll_at_ms(&self) -> Option<u64> {
        self.next_poll_ms
    }

    fn stop(&mut self) -> Result<(), DeviceError> {
        if let Some(error) = self.close_error {
            return Err(error);
        }
        if let Some(file) = &self.file {
            if self.stream_may_run {
                // Failure retains descriptor and DMA storage for explicit stop retry.
                status(unsafe { nxrs_camera_stream(file.as_raw_fd(), 0) })?;
                self.stream_may_run = false;
            }
        }
        let result = if let Some(file) = self.file.take() {
            // SAFETY: consume the sole File owner; NuttX uninstalls even a failed close.
            status(unsafe { rc_nx_close(file.into_raw_fd()) })
        } else {
            Ok(())
        };
        self.buffer = None;
        self.format = None;
        self.queued = false;
        self.ready = None;
        self.next_poll_ms = None;
        self.last_poll_ms = None;
        self.fault = None;
        self.close_error = result.err();
        result
    }
}

impl Drop for LiveCamera {
    fn drop(&mut self) {
        if self.stop().is_err() && self.stream_may_run {
            // Fail closed: never free DMA memory or admit a new owner without quiescence.
            if let Some(buffer) = self.buffer.take() {
                std::mem::forget(buffer);
            }
            if let Some(file) = self.file.take() {
                std::mem::forget(file);
            }
            return;
        }
        ACQUIRED.store(false, Ordering::Release);
    }
}

#[cfg(test)]
#[path = "v4l2_tests.rs"]
mod tests;
