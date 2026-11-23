//! Browser-side qualification exports and the host logging import.
#![allow(unsafe_code)]

#[link(wasm_import_module = "nxrs_logging")]
unsafe extern "C" {
    fn emit(ptr: *const u8, len: usize);
}

#[unsafe(no_mangle)]
pub extern "C" fn enable_diagnostics() -> u32 {
    logging::install(log::LevelFilter::Info, |_, line| {
        let bytes = line.as_bytes();
        // The import copies the bytes before returning to Wasm.
        unsafe { emit(bytes.as_ptr(), bytes.len()) };
    })
    .is_ok() as u32
}

#[unsafe(no_mangle)]
pub extern "C" fn configure_observations(with_gnss: u32) {
    super::configure(with_gnss != 0);
}
