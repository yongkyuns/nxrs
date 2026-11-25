use crate::Error;
use embedded_hal::pwm::{ErrorType, SetDutyCycle};
use std::{fs::File, num::NonZeroU32, os::fd::AsFd};

#[allow(unsafe_code)]
mod ffi;

/// A single configured PWM output. The caller owns exclusive use of the endpoint.
pub struct Output {
    file: File,
    frequency: NonZeroU32,
    active: bool,
}

impl Output {
    /// Start a waveform at the requested frequency and duty on the BSP's channel.
    /// Duty spans 0 through 65535. Actual timing is quantized by the controller.
    pub fn start(path: &str, frequency: NonZeroU32, duty: u16) -> Result<Self, Error> {
        let file = File::open(path).map_err(|_| Error::Io)?;
        ffi::configure(file.as_fd(), frequency.get(), duty)?;
        let output = Self {
            file,
            frequency,
            active: true,
        };
        ffi::start(output.file.as_fd())?;
        Ok(output)
    }

    /// Stop output and report any driver error. Closing also invokes the
    /// NuttX lower half shutdown when this is the last open descriptor.
    pub fn stop(mut self) -> Result<(), Error> {
        self.stop_output()
    }

    fn stop_output(&mut self) -> Result<(), Error> {
        if std::mem::take(&mut self.active) {
            ffi::stop(self.file.as_fd())?;
        }
        Ok(())
    }
}

impl ErrorType for Output {
    type Error = Error;
}

impl SetDutyCycle for Output {
    fn max_duty_cycle(&self) -> u16 {
        u16::MAX
    }

    fn set_duty_cycle(&mut self, duty: u16) -> Result<(), Error> {
        ffi::configure(self.file.as_fd(), self.frequency.get(), duty)
    }
}

impl Drop for Output {
    fn drop(&mut self) {
        let _ = self.stop_output();
    }
}
