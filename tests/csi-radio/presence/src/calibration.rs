//! Target adapter; calibration and runtime are shared verbatim with host replay.
pub use csi_lab_core::calibration::*;
pub static RUNTIME: Runtime = Runtime::new();
#[cfg(target_os = "espidf")]
#[no_mangle]
pub static presence_calibration_static_bytes: u32 = std::mem::size_of::<Runtime>() as u32;
pub fn now_us() -> u64 {
    extern "C" {
        fn esp_timer_get_time() -> i64;
    }
    unsafe { esp_timer_get_time() as u64 }
}
