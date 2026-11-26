//! Fallible reservations and bounded diagnostics for firmware-owned buffers.
#![forbid(unsafe_code)]
#![no_std]

extern crate std;

use core::mem::size_of;
use std::{collections::TryReserveError, io, vec::Vec};

/// Report a reservation failure in task context through the installed log sink.
/// Pass the total requested capacity in elements.
pub fn reservation_failed<T>(
    buffer: &'static str,
    capacity: usize,
    error: TryReserveError,
) -> io::ErrorKind {
    report_failure(buffer, capacity.saturating_mul(size_of::<T>()), error)
}

#[cold]
#[inline(never)]
fn report_failure(
    buffer: &'static str,
    requested_bytes: usize,
    error: TryReserveError,
) -> io::ErrorKind {
    log::error!("reservation failed buffer={buffer} requested_bytes={requested_bytes}: {error}");
    io::ErrorKind::OutOfMemory
}

pub fn try_vec<T>(buffer: &'static str, capacity: usize) -> io::Result<Vec<T>> {
    let mut values = Vec::new();
    values
        .try_reserve_exact(capacity)
        .map_err(|error| reservation_failed::<T>(buffer, capacity, error))?;
    Ok(values)
}

pub fn try_zeroed(buffer: &'static str, bytes: usize) -> io::Result<Vec<u8>> {
    let mut values = try_vec(buffer, bytes)?;
    values.resize(bytes, 0);
    Ok(values)
}
