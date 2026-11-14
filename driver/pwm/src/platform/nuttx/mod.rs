use crate::Error;
use embedded_hal::pwm::{ErrorType, SetDutyCycle};
use std::{fs::File, num::NonZeroU32, os::fd::AsFd};

#[allow(unsafe_code)]
mod ffi;

/// A single configured PWM output. The caller owns exclusive use of the endpoint.
pub struct Output {
    file: File,
    frequency: Option<NonZeroU32>,
    active: bool,
}

impl Output {
    /// Open the endpoint and explicitly stop any previous waveform. Acquiring
    /// a descriptor alone does not establish the output's initial state.
    pub fn open(path: &str) -> Result<Self, Error> {
        let file = File::open(path).map_err(|_| Error::Io)?;
        let mut output = Self {
            file,
            frequency: None,
            // Until STOP succeeds the hardware state is unknown. Drop also
            // attempts STOP if that first operation reports an error.
            active: true,
        };
        output.disable()?;
        Ok(output)
    }

    /// Start a waveform at the requested frequency and duty on the BSP's channel.
    /// Duty spans 0 through 65535. Actual timing is quantized by the controller.
    pub fn start(path: &str, frequency: NonZeroU32, duty: u16) -> Result<Self, Error> {
        let mut output = Self::open(path)?;
        output.configure(frequency, duty)?;
        Ok(output)
    }

    /// Apply a waveform and start it. Failed native operations can still have
    /// effects; retain uncertainty so disable or Drop attempts STOP.
    pub fn configure(&mut self, frequency: NonZeroU32, duty: u16) -> Result<(), Error> {
        self.active = true;
        self.frequency = None;
        ffi::configure(self.file.as_fd(), frequency.get(), duty)?;
        ffi::start(self.file.as_fd())?;
        self.frequency = Some(frequency);
        Ok(())
    }

    /// Stop output and report any driver error. Closing also invokes the
    /// NuttX lower half shutdown when this is the last open descriptor.
    pub fn stop(mut self) -> Result<(), Error> {
        self.disable()
    }

    /// Establish stopped output while retaining the descriptor for reconfigure.
    /// A failed STOP leaves the state uncertain and can be retried explicitly.
    pub fn disable(&mut self) -> Result<(), Error> {
        if self.active {
            ffi::stop(self.file.as_fd())?;
            self.active = false;
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
        let frequency = self.frequency.ok_or(Error::Unsupported)?;
        ffi::configure(self.file.as_fd(), frequency.get(), duty)
    }
}

impl Drop for Output {
    fn drop(&mut self) {
        let _ = self.disable();
    }
}
