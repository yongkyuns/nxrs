//! NuttX descriptor, message and APB layout translation.
use crate::DeviceError;
use std::ffi::CStr;
use std::num::NonZeroUsize;
use std::os::fd::{FromRawFd, OwnedFd, RawFd};

unsafe extern "C" {
    fn audio_capture_queue(name: *const std::ffi::c_char, capacity: u32) -> i32;
    fn audio_capture_unlink(name: *const std::ffi::c_char) -> i32;
    fn audio_capture_reserve(fd: i32) -> i32;
    fn audio_capture_release(fd: i32) -> i32;
    fn audio_capture_configure(fd: i32, rate: u32) -> i32;
    fn audio_capture_subscribe(fd: i32, queue: i32) -> i32;
    fn audio_capture_unsubscribe(fd: i32) -> i32;
    fn audio_capture_start(fd: i32) -> i32;
    fn audio_capture_stop(fd: i32) -> i32;
    fn audio_capture_alloc(fd: i32, bytes: u16, buffer: *mut usize) -> i32;
    fn audio_capture_free(fd: i32, buffer: usize) -> i32;
    fn audio_capture_submit(fd: i32, buffer: usize) -> i32;
    fn audio_capture_event(queue: i32, buffer: *mut usize) -> i32;
    fn audio_capture_samples(buffer: usize, bytes: u16) -> *const u8;
}

fn status(result: i32) -> Result<(), DeviceError> {
    match result {
        0 => Ok(()),
        1 => Err(DeviceError::Busy),
        _ => Err(DeviceError::Io),
    }
}

pub(super) fn queue(name: &CStr, capacity: u32) -> Result<OwnedFd, DeviceError> {
    // SAFETY: the C string is consumed synchronously.
    let fd = unsafe { audio_capture_queue(name.as_ptr(), capacity) };
    if fd < 0 {
        return Err(DeviceError::Io);
    }
    // SAFETY: NuttX mqd_t is an exclusive descriptor whose close is mq_close.
    let queue = unsafe { OwnedFd::from_raw_fd(fd) };
    // SAFETY: the name is the exclusively created queue owned by this call.
    status(unsafe { audio_capture_unlink(name.as_ptr()) })?;
    Ok(queue)
}

pub(super) fn reserve(fd: RawFd) -> Result<(), DeviceError> {
    // SAFETY: synchronous control on the owned audio descriptor.
    status(unsafe { audio_capture_reserve(fd) })
}

pub(super) fn release(fd: RawFd) -> Result<(), DeviceError> {
    // SAFETY: no submitted buffers remain and fd is owned.
    status(unsafe { audio_capture_release(fd) })
}

pub(super) fn configure(fd: RawFd, rate: u32) -> Result<(), DeviceError> {
    // SAFETY: exclusive descriptor and synchronous format configuration.
    status(unsafe { audio_capture_configure(fd, rate) })
}

pub(super) fn subscribe(fd: RawFd, queue: RawFd) -> Result<(), DeviceError> {
    // SAFETY: both descriptors are owned until unsubscribe or quarantine.
    status(unsafe { audio_capture_subscribe(fd, queue) })
}

pub(super) fn unsubscribe(fd: RawFd) -> Result<(), DeviceError> {
    // SAFETY: capture has completed or never started.
    status(unsafe { audio_capture_unsubscribe(fd) })
}

pub(super) fn start(fd: RawFd) -> Result<(), DeviceError> {
    // SAFETY: exclusive configured descriptor.
    status(unsafe { audio_capture_start(fd) })
}

pub(super) fn stop(fd: RawFd) -> Result<(), DeviceError> {
    // SAFETY: exclusive descriptor. Busy leaves native ownership unchanged.
    status(unsafe { audio_capture_stop(fd) })
}

pub(super) struct Buffer(NonZeroUsize);

impl Buffer {
    pub(super) fn allocate(fd: RawFd, bytes: u16) -> Result<Self, DeviceError> {
        let mut pointer = 0;
        // SAFETY: exclusive descriptor and initialized writable pointer output.
        status(unsafe { audio_capture_alloc(fd, bytes, &mut pointer) })?;
        NonZeroUsize::new(pointer)
            .map(Self)
            .ok_or(DeviceError::InvalidData)
    }

    pub(super) fn token(&self) -> usize {
        self.0.get()
    }

    pub(super) fn free(&self, fd: RawFd) -> Result<(), DeviceError> {
        // SAFETY: this APB owner is removed only after FREEBUFFER succeeds.
        status(unsafe { audio_capture_free(fd, self.token()) })
    }

    pub(super) fn submit(&self, fd: RawFd) -> Result<(), DeviceError> {
        // SAFETY: Rust checked that this buffer is currently outside DMA.
        status(unsafe { audio_capture_submit(fd, self.token()) })
    }

    pub(super) fn samples(&self, bytes: u16) -> Result<&[u8], DeviceError> {
        // SAFETY: an admitted dequeue guarantees that DMA finished and this APB
        // remains allocated by the capture owner. The shim checks its length.
        let data = unsafe { audio_capture_samples(self.token(), bytes) };
        if data.is_null() {
            return Err(DeviceError::InvalidData);
        }
        // SAFETY: the checked view cannot outlive the buffer or another submit.
        Ok(unsafe { std::slice::from_raw_parts(data, usize::from(bytes)) })
    }
}

pub(super) fn receive(queue: RawFd) -> Result<usize, DeviceError> {
    let mut pointer = 0;
    // SAFETY: live queue and initialized writable output.
    match unsafe { audio_capture_event(queue, &mut pointer) } {
        0 => Err(DeviceError::Closed),
        1 => Ok(pointer),
        2 => Err(DeviceError::Busy),
        -2 => Err(DeviceError::InvalidData),
        _ => Err(DeviceError::Io),
    }
}
