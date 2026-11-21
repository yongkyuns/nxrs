use super::{station, Field};
use core::ffi::{c_char, c_int};

unsafe extern "C" {
    pub(super) fn wifi_station_available() -> c_int;
    pub(super) fn wifi_quiescence_available() -> c_int;
    pub(super) fn wifi_interface_flags_get(
        fd: c_int,
        interface: *const c_char,
        flags: *mut u32,
        up_mask: *mut u32,
    ) -> c_int;
    pub(super) fn wifi_interface_flags_set(
        fd: c_int,
        interface: *const c_char,
        flags: u32,
    ) -> c_int;
    pub(super) fn wifi_station_link(fd: c_int, interface: *const c_char, state: *mut u32) -> c_int;
    pub(super) fn wifi_station_infra(fd: c_int, interface: *const c_char) -> c_int;
    pub(super) fn wifi_station_wpa(fd: c_int, interface: *const c_char, secured: c_int) -> c_int;
    pub(super) fn wifi_station_cipher(fd: c_int, interface: *const c_char, secured: c_int)
        -> c_int;
    pub(super) fn wifi_station_key(
        fd: c_int,
        interface: *const c_char,
        password: *const u8,
        length: usize,
    ) -> c_int;
    pub(super) fn wifi_station_ssid(
        fd: c_int,
        interface: *const c_char,
        ssid: *const u8,
        length: usize,
    ) -> c_int;
    pub(super) fn wifi_station_disconnect(fd: c_int, interface: *const c_char) -> c_int;
    pub(super) fn wifi_station_address(interface: *const c_char, field: u32, value: u32) -> c_int;
    pub(super) fn wifi_station_mac(interface: *const c_char, mac: *mut u8) -> c_int;
    pub(super) fn wifi_dhcp_open(
        interface: *const c_char,
        mac: *const u8,
        client: *mut *mut core::ffi::c_void,
    ) -> c_int;
    pub(super) fn wifi_dhcp_request(
        client: *mut core::ffi::c_void,
        lease: *mut station::Lease,
    ) -> c_int;
    pub(super) fn wifi_dhcp_close(client: *mut core::ffi::c_void);
    pub(super) fn wifi_scan_check(fd: c_int, interface: *const c_char) -> c_int;
    pub(super) fn wifi_scan_start(fd: c_int, interface: *const c_char) -> c_int;
    pub(super) fn wifi_scan_read(
        fd: c_int,
        interface: *const c_char,
        buffer: *mut u8,
        capacity: usize,
        length: *mut usize,
    ) -> c_int;
    pub(super) fn wifi_scan_next(
        buffer: *const u8,
        length: usize,
        offset: *mut usize,
        field: *mut Field,
    ) -> c_int;
    pub(super) fn wifi_initialize_network() -> i32;
}
