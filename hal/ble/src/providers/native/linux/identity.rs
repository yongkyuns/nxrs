//! BlueZ daemon-scoped path identities and canonical UUID text.
use super::gio;
use std::prelude::v1::*;
pub(super) fn identity(daemon: &str, path: &str) -> [u8; 16] {
    let hash = gio::checksum(&[daemon.as_bytes(), b"\0", path.as_bytes()]);
    hash[..16].try_into().unwrap()
}
pub(super) fn uuid(bytes: &[u8; 16]) -> String {
    let mut out = String::with_capacity(36);
    use std::fmt::Write;
    for (i, byte) in bytes.iter().enumerate() {
        if [4, 6, 8, 10].contains(&i) {
            out.push('-');
        }
        write!(out, "{byte:02x}").unwrap();
    }
    out
}
#[cfg(test)]
#[no_mangle]
unsafe extern "C" fn bluez_uuid(bytes: *const u8, out: *mut u8) {
    let bytes = unsafe { &*bytes.cast::<[u8; 16]>() };
    let text = uuid(bytes);
    unsafe {
        std::ptr::copy_nonoverlapping(text.as_ptr(), out, 36);
        out.add(36).write(0);
    }
}
#[cfg(test)]
#[no_mangle]
unsafe extern "C" fn bluez_identity(
    daemon: *const std::ffi::c_char,
    path: *const std::ffi::c_char,
    out: *mut u8,
) {
    let token = identity(&unsafe { gio::borrowed_text(daemon) }, &unsafe {
        gio::borrowed_text(path)
    });
    unsafe { std::ptr::copy_nonoverlapping(token.as_ptr(), out, 16) }
}
