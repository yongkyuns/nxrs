unsafe extern "C" {
    pub(super) fn config_get(fd: i32, key: *const u8, data: *mut u8, length: *mut usize) -> i32;
    pub(super) fn config_set(fd: i32, key: *const u8, data: *const u8, length: usize) -> i32;
    pub(super) fn config_remove(fd: i32, key: *const u8) -> i32;
}
