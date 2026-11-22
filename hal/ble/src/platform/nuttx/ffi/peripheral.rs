use std::ffi::c_void;
use std::ptr::NonNull;

unsafe extern "C" {
    pub(super) fn peripheral_register(uuids: *const u8);
    pub(super) fn peripheral_start() -> i32;
    pub(super) fn peripheral_stop();
    fn bt_conn_lookup_handle(handle: u16) -> *mut c_void;
    fn bt_conn_release(connection: *mut c_void);
    fn peripheral_disconnect(connection: *mut c_void);
}

pub(super) struct Peer(NonNull<c_void>);

impl Peer {
    pub(super) fn lookup(handle: u16) -> Option<Self> {
        // The NuttX host retains the connection for the returned reference.
        NonNull::new(unsafe { bt_conn_lookup_handle(handle) }).map(Self)
    }

    pub(super) fn disconnect(&self) {
        unsafe { peripheral_disconnect(self.0.as_ptr()) };
    }
}

impl Drop for Peer {
    fn drop(&mut self) {
        unsafe { bt_conn_release(self.0.as_ptr()) };
    }
}
