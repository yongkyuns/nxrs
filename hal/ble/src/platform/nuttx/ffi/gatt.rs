use core::ffi::c_void;

unsafe extern "C" {
    pub(super) fn gatt_lock() -> usize;
    pub(super) fn gatt_unlock(flags: usize);
    pub(super) fn gatt_schedule(ms: u32) -> i32;
    pub(super) fn gatt_unschedule();
    pub(super) fn gatt_error(kind: u32) -> i32;
    pub(super) fn gatt_register() -> i32;
    pub(super) fn gatt_unregister() -> i32;
    pub(super) fn gatt_lookup(address: *const u8, kind: u8) -> *mut c_void;
    pub(super) fn gatt_create(address: *const u8, kind: u8) -> *mut c_void;
    pub(super) fn gatt_release_conn(conn: *mut c_void);
    pub(super) fn gatt_conn_quiet(conn: *mut c_void) -> i32;
    pub(super) fn gatt_cancel(conn: *mut c_void);
    pub(super) fn gatt_disconnect(conn: *mut c_void) -> i32;
    pub(super) fn gatt_mtu(conn: *mut c_void) -> u16;
    pub(super) fn gatt_discover(
        conn: *mut c_void,
        characteristic: i32,
        start: u16,
        end: u16,
        uuid: *const u8,
    ) -> i32;
    pub(super) fn gatt_read(conn: *mut c_void, handle: u16, offset: u16) -> i32;
    pub(super) fn gatt_write(conn: *mut c_void, handle: u16, data: *const u8, length: u16) -> i32;
}
