//! NuttX syslog owns delivery to the configured OS channels.
#![allow(unsafe_code)]

use log::{Level, LevelFilter, SetLoggerError};
use std::ffi::{c_char, c_int};

unsafe extern "C" {
    fn syslog(priority: c_int, format: *const c_char, ...);
}

pub fn install(level: LevelFilter) -> Result<(), SetLoggerError> {
    logging::install(level, |severity, line| {
        let priority: c_int = match severity {
            Level::Error => 3,
            Level::Warn => 4,
            Level::Info => 6,
            Level::Debug | Level::Trace => 7,
        };
        let bytes = line.as_bytes();
        // The precision bounds the read to the live byte slice. The format
        // string is static and the call consumes the bytes synchronously.
        unsafe {
            syslog(
                priority,
                c"%.*s".as_ptr(),
                bytes.len() as c_int,
                bytes.as_ptr().cast::<c_char>(),
            )
        }
    })
}
