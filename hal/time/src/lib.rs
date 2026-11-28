//! Exclusive control of the system wall clock. Read time and wait directly with
//! `std::time` and `std::thread`; source selection and synchronization policy
//! belong to the caller.
#![deny(unsafe_code)]

use std::time::SystemTime;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// The provider or requested operation is unavailable.
    Unsupported,
    /// This linked image already has an owner.
    Busy,
    /// A time or correction cannot be represented by the provider.
    OutOfRange,
    /// Native failure or an invalid native response. An operation may already
    /// have taken effect; this error does not promise rollback.
    Io,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Capabilities {
    /// Maximum software correction rate in parts per million. `None` means
    /// adjustment, query and cancellation are unsupported. This describes
    /// configured behavior, not oscillator accuracy or sleep retention.
    pub slew_rate_ppm: Option<u32>,
}

/// One exclusive wall-clock writer. Operations do not change the monotonic
/// clock. Dropping the handle releases admission and leaves any correction in
/// progress; acquire again to query, replace or cancel it explicitly.
///
/// Admission coordinates users of this HAL within one linked Rust image.
/// Native clock writers, other processes and RTC setters must be coordinated
/// by the caller; acquiring this handle does not fence them out.
pub trait Control {
    fn capabilities(&self) -> Capabilities;

    /// Step UTC wall time, including pre-epoch times representable by the
    /// provider. Success clears a correction managed by this capability. The
    /// OS may also update its hardware RTC; success does not confirm that
    /// retention write.
    fn set(&mut self, time: SystemTime) -> Result<(), Error>;

    /// Replace the remaining correction with signed whole microseconds,
    /// returning the previous residual in the same units. Positive values
    /// speed up wall time; negative values slow it without reversing it.
    /// Already applied progress is preserved, and invalid requests leave the
    /// current correction unchanged. Zero explicitly cancels a correction.
    fn replace_adjustment(&mut self, correction_us: i64) -> Result<i64, Error>;

    /// Query without replacing the correction. A fractional microsecond is
    /// rounded away from zero, so zero means the correction has completed.
    fn pending_adjustment(&mut self) -> Result<i64, Error>;

    /// Cancel after accounting for progress already applied, returning the
    /// remaining signed microseconds that were cancelled.
    fn cancel_adjustment(&mut self) -> Result<i64, Error> {
        self.replace_adjustment(0)
    }
}

mod platform;
pub use platform::open;
