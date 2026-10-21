//! NuttX acquisition and scan normalization. Portable execution lives in worker.
#![deny(unsafe_code)]

pub(crate) use crate::worker::Lease;
use crate::{AccessPoint, Error, Security, Ssid};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(not(test))]
mod native;

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
