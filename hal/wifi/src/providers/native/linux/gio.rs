//! Capability-local symbol wiring for the shared Linux GIO protocol binding.
unsafe extern "C" {
    #[link_name = "nxrs_wifi_gio_constant"]
    fn native_constant(kind: u32) -> i32;
    #[link_name = "nxrs_wifi_gio_error_class"]
    fn error_class(error: *mut core::ffi::c_void) -> u32;
    #[link_name = "nxrs_wifi_gio_error_text"]
    fn error_text(error: *mut core::ffi::c_void) -> *mut core::ffi::c_char;
}

#[path = "../../../../../../driver/linux/gio.rs"]
mod binding;
pub use binding::*;
