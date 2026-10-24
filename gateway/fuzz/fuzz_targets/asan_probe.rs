//! Test-only sanitizer witness: deliberately misuse an owned, synthetic allocation.
//! This is never linked into a gateway or firmware executable.
#![no_main]
use libfuzzer_sys::fuzz_target;
fuzz_target!(|_data: &[u8]| {
    let bytes = vec![0x55u8; 64];
    let pointer = std::hint::black_box(bytes.as_ptr());
    drop(bytes);
    // Deliberate local test failure. An actual ASan diagnostic is mandatory.
    unsafe {
        std::hint::black_box(std::ptr::read_volatile(pointer));
    }
});
