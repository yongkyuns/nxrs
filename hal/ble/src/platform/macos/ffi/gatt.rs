use core::ffi::c_void;

unsafe extern "C" {
    pub(super) fn mac_gatt_initialize();
    pub(super) fn mac_gatt_allocate();
    pub(super) fn mac_gatt_poke();
    pub(super) fn mac_gatt_defer();
    pub(super) fn mac_gatt_errno(kind: u32) -> i32;
    pub(super) fn mac_gatt_authorization() -> i32;
    pub(super) fn mac_gatt_manager();
    pub(super) fn mac_gatt_manager_state() -> u32;
    pub(super) fn mac_gatt_object(field: u32, index: usize) -> *mut c_void;
    pub(super) fn mac_gatt_count(field: u32) -> usize;
    pub(super) fn mac_gatt_identity(object: *mut c_void, bytes: *mut u8);
    pub(super) fn mac_gatt_uuid(
        object: *mut c_void,
        characteristic: bool,
        bytes: *mut *const u8,
        len: *mut usize,
    );
    pub(super) fn mac_gatt_retrieve(
        identity: *const u8,
        index: usize,
        count: *mut usize,
    ) -> *mut c_void;
    pub(super) fn mac_gatt_scan();
    pub(super) fn mac_gatt_stop_scan();
    pub(super) fn mac_gatt_connect(peripheral: *mut c_void);
    pub(super) fn mac_gatt_connected() -> bool;
    pub(super) fn mac_gatt_cancel();
    pub(super) fn mac_gatt_services(uuid: *const u8);
    pub(super) fn mac_gatt_characteristics(service: *mut c_void, uuid: *const u8);
    pub(super) fn mac_gatt_pin(attribute: *mut c_void);
    pub(super) fn mac_gatt_properties(attribute: *mut c_void) -> u32;
    pub(super) fn mac_gatt_max_write() -> usize;
    pub(super) fn mac_gatt_read(attribute: *mut c_void);
    pub(super) fn mac_gatt_write(attribute: *mut c_void, data: *const u8, len: usize);
    pub(super) fn mac_gatt_detach(release: bool);
}
