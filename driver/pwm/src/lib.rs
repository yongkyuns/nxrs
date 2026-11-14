//! Single-channel PWM through a NuttX timer endpoint.
#![deny(unsafe_code)]

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    Unsupported,
    Io,
}

impl embedded_hal::pwm::Error for Error {
    fn kind(&self) -> embedded_hal::pwm::ErrorKind {
        embedded_hal::pwm::ErrorKind::Other
    }
}

#[cfg(target_os = "nuttx")]
mod platform;
#[cfg(target_os = "nuttx")]
pub use platform::nuttx::Output;
