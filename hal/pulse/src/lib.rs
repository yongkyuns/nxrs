//! Controlled pulse output with task-context edge observations.
//!
//! One profile binds a PWM output and a rising-edge input. The factory acquires
//! them exclusively and establishes stopped output before returning. An explicit
//! mock uses a timer; unbound builds return Unsupported. Neither mock events nor
//! GPIO receipt times establish electrical timing or an exact hardware edge count.
#![deny(unsafe_code)]

mod api;
pub use api::{Error, Event, Session, Stats, Waveform};
mod platform;
#[cfg(any(platform = "mock", platform = "nuttx", test))]
mod runtime;
pub use platform::open;
