//! ADC sample I/O through the NuttX ADC character device.
#![deny(unsafe_code)]

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Sample {
    pub channel: u8,
    /// Native driver units. The endpoint determines whether this is a
    /// converter count or a calibrated value.
    pub value: i32,
}

#[cfg(target_os = "nuttx")]
mod platform;
#[cfg(target_os = "nuttx")]
pub use platform::nuttx::Adc;
