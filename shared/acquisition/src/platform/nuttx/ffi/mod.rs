//! Target poll/pipe ABI. Borrowed descriptors stay alive through each call.
use std::{
    ffi::{c_int, c_long},
    io,
};

// Verified against the selected NuttX headers by events.c.
const CLOCK_MONOTONIC: c_int = 1;
#[repr(C)]
struct Timespec {
    seconds: i64,
    nanoseconds: c_long,
}

unsafe extern "C" {
    fn clock_gettime(clock_id: c_int, time: *mut Timespec) -> c_int;
}

/// The boot monotonic epoch also used by kernel timestamped GPIO edges.
pub fn monotonic_us() -> u64 {
    let mut time = std::mem::MaybeUninit::<Timespec>::uninit();
    // SAFETY: the clock ID is valid and time points to writable native storage.
    let status = unsafe { clock_gettime(CLOCK_MONOTONIC, time.as_mut_ptr()) };
    assert_eq!(status, 0, "read CLOCK_MONOTONIC");
    // SAFETY: a successful clock_gettime initialized the timespec.
    let time = unsafe { time.assume_init() };
    time.seconds as u64 * 1_000_000 + time.nanoseconds as u64 / 1_000
}
/// Own newly created descriptors immediately; native code retains neither.
pub fn pipe() -> io::Result<(std::fs::File, std::fs::File)> {
    use std::os::fd::FromRawFd;
    let mut descriptors = [-1; 2];
    // SAFETY: two writable int slots for this synchronous pipe2 call.
    if unsafe { acquisition_event_pipe(descriptors.as_mut_ptr()) } < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: pipe2 transferred two fresh, distinct owned descriptors.
    Ok(unsafe {
        (
            std::fs::File::from_raw_fd(descriptors[0]),
            std::fs::File::from_raw_fd(descriptors[1]),
        )
    })
}
pub fn wait(device: c_int, cancel: c_int) -> io::Result<bool> {
    // SAFETY: descriptors remain borrowed/alive through the synchronous poll.
    match unsafe { acquisition_event_wait(device, cancel) } {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(io::Error::last_os_error()),
    }
}

#[derive(Clone, Copy)]
pub enum Readiness {
    Device,
    Cancelled,
    Timeout,
}

/// Cancellation wins when both descriptors are readable. An absent cancel
/// descriptor is ignored by poll. The caller owns the absolute deadline.
pub fn poll(device: c_int, cancel: Option<c_int>, timeout_ms: c_int) -> io::Result<Readiness> {
    // SAFETY: synchronous poll over live descriptors, with -1 as the absent fd.
    match unsafe { acquisition_event_poll(device, cancel.unwrap_or(-1), timeout_ms) } {
        0 => Ok(Readiness::Cancelled),
        1 => Ok(Readiness::Device),
        2 => Ok(Readiness::Timeout),
        _ => Err(io::Error::last_os_error()),
    }
}

/// Read readiness, an optional pending write, a wake descriptor and one deadline.
/// The caller drains a control wake and separately checks its stop/request state.
pub fn poll_io(
    device: c_int,
    wake: c_int,
    writable: bool,
    timeout_ms: c_int,
) -> io::Result<Readiness> {
    // SAFETY: synchronous poll over live, borrowed descriptors.
    match unsafe { acquisition_event_poll_io(device, wake, writable.into(), timeout_ms) } {
        0 => Ok(Readiness::Cancelled),
        1 => Ok(Readiness::Device),
        2 => Ok(Readiness::Timeout),
        _ => Err(io::Error::last_os_error()),
    }
}
unsafe extern "C" {
    fn acquisition_event_pipe(descriptors: *mut c_int) -> c_int;
    fn acquisition_event_wait(device: c_int, cancel: c_int) -> c_int;
    fn acquisition_event_poll(device: c_int, cancel: c_int, timeout_ms: c_int) -> c_int;
    fn acquisition_event_poll_io(
        device: c_int,
        wake: c_int,
        writable: c_int,
        timeout_ms: c_int,
    ) -> c_int;
}

pub fn tick(cancel: c_int, timeout_ms: c_int) -> io::Result<bool> {
    // SAFETY: borrowed live descriptor and a bounded poll timeout, no retained data.
    match unsafe { acquisition_event_tick(cancel, timeout_ms) } {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(io::Error::last_os_error()),
    }
}
unsafe extern "C" {
    fn acquisition_event_tick(cancel: c_int, timeout_ms: c_int) -> c_int;
}
