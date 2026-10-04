//! Allocation-free CPU probes; kernels are shared with the queued service fixture.
#![cfg_attr(all(target_os = "nuttx", not(test)), no_main)]
mod payload_kernels;
pub use payload_kernels::*;

#[cfg(all(target_os = "nuttx", not(test)))]
unsafe extern "C" {
    fn nxrs_payload_run(
        encode: unsafe extern "C" fn(*mut u8, usize, u32, *const i16) -> i32,
        parse: unsafe extern "C" fn(*const u8, usize, *mut i16, *mut u32) -> i32,
        filter: unsafe extern "C" fn(*const i16, *mut i32, *mut i32),
        filter_xyz: unsafe extern "C" fn(*const i16, *mut i32, *mut i32),
    ) -> i32;
}

#[cfg(all(target_os = "nuttx", not(test)))]
#[unsafe(no_mangle)]
pub extern "C" fn main(_argc: i32, _argv: *const *const u8) -> i32 {
    // SAFETY: the synchronous C runner owns every buffer and validates results.
    // Passing real entry addresses also roots them through the Rust partial
    // link's unused-section removal; no synthetic keep-alive code is needed.
    unsafe {
        nxrs_payload_run(
            nxrs_rust_encode,
            nxrs_rust_parse,
            nxrs_rust_filter,
            nxrs_rust_filter_xyz,
        )
    }
}

#[cfg(not(all(target_os = "nuttx", not(test))))]
fn main() {}
