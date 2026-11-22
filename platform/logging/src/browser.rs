//! Hand each line to the host before the borrowed Wasm bytes expire.
#![allow(unsafe_code)]

use log::{LevelFilter, SetLoggerError};

#[link(wasm_import_module = "nxrs_logging")]
unsafe extern "C" {
    fn emit(ptr: *const u8, len: usize);
}

pub fn install(level: LevelFilter) -> Result<(), SetLoggerError> {
    logging::install(level, |_, line| {
        let bytes = line.as_bytes();
        // The browser import copies the bytes synchronously into bounded storage.
        unsafe { emit(bytes.as_ptr(), bytes.len()) };
    })
}
