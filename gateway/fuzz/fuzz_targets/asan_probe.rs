//! Test-only positive control. Never linked into a gateway or firmware binary.
#![no_main]
use libfuzzer_sys::fuzz_target;
fuzz_target!(|_data: &[u8]| {
    let bytes = vec![0x55u8; 64];
    let pointer = std::hint::black_box(bytes.as_ptr());
    drop(bytes);
    // Deliberately misuse this synthetic owned allocation. Qualification must
    // observe a real AddressSanitizer heap-use-after-free failure.
    unsafe {
        std::hint::black_box(std::ptr::read_volatile(pointer));
    }
});
