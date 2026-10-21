use super::*;
#[no_mangle]
unsafe extern "C" fn bluez_uuid(bytes: *const u8, out: *mut u8) {
    let bytes = unsafe { &*bytes.cast::<[u8; 16]>() };
    let text = uuid(bytes);
    unsafe {
        std::ptr::copy_nonoverlapping(text.as_ptr(), out, 36);
        out.add(36).write(0);
    }
}
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
