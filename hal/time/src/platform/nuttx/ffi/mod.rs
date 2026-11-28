use crate::{Capabilities, Error};

unsafe extern "C" {
    fn time_control_capabilities(slew_rate_ppm: *mut u32) -> i32;
    fn time_set_realtime(seconds: i64, nanoseconds: u32) -> i32;
    fn time_adjust_realtime(
        replace: bool,
        seconds: i64,
        microseconds: i32,
        previous_seconds: *mut i64,
        previous_microseconds: *mut i32,
    ) -> i32;
}

pub(super) fn capabilities() -> Result<Capabilities, Error> {
    let mut rate = 0;
    // SAFETY: the initialized output remains valid for this synchronous call.
    result(unsafe { time_control_capabilities(&mut rate) })?;
    Ok(Capabilities {
        slew_rate_ppm: (rate != 0).then_some(rate),
    })
}

pub(super) fn set(seconds: i64, nanoseconds: u32) -> Result<(), Error> {
    // SAFETY: Rust validated the normalized scalar time; C owns native layout.
    result(unsafe { time_set_realtime(seconds, nanoseconds) })
}

pub(super) fn adjust(replace: bool, seconds: i64, microseconds: i32) -> Result<(i64, i32), Error> {
    let mut previous_seconds = 0;
    let mut previous_microseconds = 0;
    // SAFETY: inputs are normalized and validated; both distinct outputs remain
    // valid throughout the synchronous call. C owns the native timeval layout.
    result(unsafe {
        time_adjust_realtime(
            replace,
            seconds,
            microseconds,
            &mut previous_seconds,
            &mut previous_microseconds,
        )
    })?;
    Ok((previous_seconds, previous_microseconds))
}

fn result(status: i32) -> Result<(), Error> {
    match status {
        0 => Ok(()),
        1 => Err(Error::Unsupported),
        2 => Err(Error::OutOfRange),
        _ => Err(Error::Io),
    }
}
