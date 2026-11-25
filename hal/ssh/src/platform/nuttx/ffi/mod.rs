use std::os::unix::ffi::OsStrExt;
use std::{
    ffi::{c_char, c_int, CStr},
    io,
    path::Path,
};

unsafe extern "C" {
    fn maintenance_spawn() -> c_int;
    fn maintenance_reaped(pid: c_int) -> c_int;
    fn maintenance_hostkey() -> *const c_char;
    fn maintenance_passwd() -> *const c_char;
    fn maintenance_bind_address() -> *const c_char;
    fn dropbear_service_cancel_owner(pid: c_int);
}

fn result(value: c_int) -> io::Result<c_int> {
    if value < 0 {
        Err(io::Error::from_raw_os_error(-value))
    } else {
        Ok(value)
    }
}

pub(super) fn spawn() -> io::Result<i32> {
    result(unsafe { maintenance_spawn() })
}

pub(super) fn reaped(pid: i32) -> io::Result<bool> {
    let value = unsafe { maintenance_reaped(pid) };
    result(value).map(|found| found != 0)
}

pub(super) fn request_stop(pid: i32) {
    unsafe { dropbear_service_cancel_owner(pid) }
}

pub(super) fn credential_paths() -> [&'static Path; 2] {
    // Both pointers refer to static Kconfig string constants.
    unsafe { [maintenance_hostkey(), maintenance_passwd()] }.map(|ptr| {
        Path::new(std::ffi::OsStr::from_bytes(
            unsafe { CStr::from_ptr(ptr) }.to_bytes(),
        ))
    })
}

pub(super) fn bind_address() -> &'static str {
    // Kconfig emits a static string literal.
    unsafe { CStr::from_ptr(maintenance_bind_address()) }
        .to_str()
        .unwrap_or("")
}
