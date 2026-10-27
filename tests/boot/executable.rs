//! Emulator-only application: a signed entry must execute a volatile write.
#![no_std]
#![deny(unsafe_op_in_unsafe_fn)]

#[no_mangle]
static mut NXRS_ENTRY_WITNESS: u32 = 0x11223344;

#[no_mangle]
pub extern "C" fn fixture_entry() -> ! {
    // SAFETY: single CPU, test-owned static; no reference or concurrent access.
    unsafe { core::ptr::write_volatile(core::ptr::addr_of_mut!(NXRS_ENTRY_WITNESS), 0x4e585253) };
    fixture_park()
}

#[no_mangle]
#[inline(never)]
pub extern "C" fn fixture_park() -> ! {
    loop {
        core::hint::spin_loop();
    }
}

#[panic_handler]
fn panic(_: &core::panic::PanicInfo<'_>) -> ! {
    loop {
        core::hint::spin_loop();
    }
}
