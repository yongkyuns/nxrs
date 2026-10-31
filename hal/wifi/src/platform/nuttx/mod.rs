//! NuttX acquisition and scan normalization. Portable execution lives in worker.
#![deny(unsafe_code)]

pub(crate) use crate::worker::Lease;
mod station;
use crate::{AccessPoint, Error, Security, Ssid};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(not(test))]
mod native;

#[cfg(not(test))]
pub(super) fn open_for_quiescence() -> Result<impl crate::QuiescenceControl, Error> {
    let lease = Lease::acquire()?;
    let interface = native::QuiescenceInterface::open()?;
    Ok(super::quiescence::Owner::new(interface, lease))
}

fn set_interface_enabled(
    enabled: bool,
    mut get: impl FnMut() -> Result<(u32, u32), Error>,
    mut set: impl FnMut(u32) -> Result<(), Error>,
) -> Result<(), Error> {
    let (flags, mask) = get()?;
    // A skipped native transition cannot prove completion of an earlier,
    // unknown operation. Only the exclusive owner's idempotence is trusted.
    if !mask.is_power_of_two() || (flags & mask != 0) == enabled {
        return Err(Error::Protocol);
    }
    // Preserve unrelated flags such as IFF_NOARP using the target's UP mask.
    set(if enabled { flags | mask } else { flags & !mask })?;
    let (observed, observed_mask) = get()?;
    if observed_mask != mask || (observed & mask != 0) != enabled {
        return Err(Error::Protocol);
    }
    Ok(())
}

pub(crate) const SCAN_READ_TIMEOUT: Duration = Duration::from_secs(6);
const SCAN_READ_POLL: Duration = Duration::from_millis(20);

#[cfg(not(test))]
pub(crate) fn open<S>(sink: S) -> Result<crate::worker::Worker, Error>
where
    S: crate::EventSink<crate::Event> + Send + 'static,
{
    let lease = Lease::acquire()?;
    let driver = native::Native::open()?;
    crate::worker::Worker::start_with_stack(driver, sink, lease, 8192)
}

/// Retry pending reads, never restart an accepted scan. A blocking ioctl itself
/// is not preemptible; the deadline bounds only the interval between calls.
pub(crate) fn read_scan_with_retry(
    mut read: impl FnMut() -> Result<usize, Error>,
    timeout: Duration,
) -> Result<usize, Error> {
    let deadline = Instant::now() + timeout;
    loop {
        match read() {
            Err(Error::Busy) => {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Err(Error::Timeout);
                }
                thread::sleep(SCAN_READ_POLL.min(remaining));
            }
            result => return result,
        }
    }
}

/// Private bridge values, not a portable scan or consumer API.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub(crate) struct Field {
    pub kind: u32,
    pub flags: u32,
    pub length: u32,
    pub data: [u8; 32],
}

pub(crate) fn normalize_scan(
    length: usize,
    mut next: impl FnMut(&mut usize) -> Result<Option<Field>, Error>,
    emit: &mut dyn FnMut(AccessPoint) -> bool,
) -> Result<bool, Error> {
    let mut offset = 0;
    let mut point = None;
    while offset < length {
        let previous = offset;
        let field = next(&mut offset)?.ok_or(Error::Protocol)?;
        if offset <= previous || offset > length {
            return Err(Error::Protocol);
        }
        if field.kind == 1 {
            if let Some(point) = point.take() {
                if !emit(point) {
                    return Ok(false);
                }
            }
            point = Some(AccessPoint {
                ssid: None,
                security: Security::Other,
                rssi_dbm: None,
            });
        } else if let Some(point) = point.as_mut() {
            match field.kind {
                2 => {
                    let ssid = field
                        .data
                        .get(..field.length as usize)
                        .ok_or(Error::Protocol)?;
                    point.ssid = if ssid.is_empty() {
                        None
                    } else {
                        Some(Ssid::new(ssid).map_err(|_| Error::Protocol)?)
                    };
                }
                3 => {
                    point.rssi_dbm = if field.flags & 3 == 3 {
                        Some(i16::from(field.data[0] as i8))
                    } else {
                        None
                    };
                }
                4 => {
                    point.security = if field.flags & 1 != 0 {
                        Security::Open
                    } else {
                        Security::Other
                    };
                }
                _ => {}
            }
        } else if field.kind != 0 {
            return Err(Error::Protocol);
        }
    }
    Ok(point.is_none_or(emit))
}

#[cfg(test)]
mod quiescence_tests {
    use super::*;
    use std::cell::Cell;

    #[test]
    fn interface_transition_requires_native_completion_and_matching_readback() {
        const UP: u32 = 1 << 1;
        const OTHER: u32 = 1 << 29;
        // Each fallible boundary, invalid masks, skipped transitions and both
        // readback inconsistencies must fail without an extra native operation.
        for case in 0..9 {
            let (flags, calls) = (Cell::new(UP | OTHER), Cell::new(0));
            let result = set_interface_enabled(
                false,
                || {
                    let call = calls.get() + 1;
                    calls.set(call);
                    if (case == 1 && call == 1) || (case == 3 && call == 3) {
                        return Err(Error::Io);
                    }
                    let mask = match (case, call) {
                        (4, 1) => 0,
                        (5, 1) => UP | 1,
                        (7, 3) => 1,
                        _ => UP,
                    };
                    let value = match (case, call) {
                        (6, 1) => OTHER,
                        (8, 3) => UP | OTHER,
                        _ => flags.get(),
                    };
                    Ok((value, mask))
                },
                |value| {
                    calls.set(calls.get() + 1);
                    assert_eq!(value, OTHER);
                    flags.set(value); // Even a failing SET may have side effects.
                    if case == 2 {
                        Err(Error::Io)
                    } else {
                        Ok(())
                    }
                },
            );
            assert_eq!(
                result,
                match case {
                    0 => Ok(()),
                    1..=3 => Err(Error::Io),
                    _ => Err(Error::Protocol),
                }
            );
            assert_eq!(
                calls.get(),
                match case {
                    1 | 4..=6 => 1,
                    2 => 2,
                    _ => 3,
                }
            );
            if case == 0 {
                assert_eq!(
                    set_interface_enabled(
                        true,
                        || Ok((flags.get(), UP)),
                        |value| {
                            flags.set(value);
                            Ok(())
                        }
                    ),
                    Ok(())
                );
                assert_eq!(flags.get(), UP | OTHER);
            }
        }
    }
}
