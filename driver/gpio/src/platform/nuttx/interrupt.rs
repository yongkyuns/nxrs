use super::{ffi, Error};
use std::{
    fs::{File, OpenOptions},
    os::fd::AsRawFd,
};

/// An owned rising-edge GPIO registration. ISR notification storage belongs to
/// NuttX; no Rust function or context is registered in interrupt context.
pub struct Interrupt<F> {
    file: File,
    registered: bool,
    context: F,
    on_cleanup_failure: fn(&F),
}

impl<F: Fn()> Interrupt<F> {
    /// The supplied hook runs synchronously in task context if registration
    /// rollback or teardown fails, including during Drop. The owner must retain
    /// that uncertainty and prevent unsafe reacquisition. The hook must not panic.
    /// Only rising-edge endpoints are currently supported.
    pub fn open_rising(path: &str, on_cleanup_failure: F) -> Result<Self, Error> {
        Self::open_rising_with_context(path, on_cleanup_failure, |notify| notify())
    }
}

impl<F> Interrupt<F> {
    /// Retain typed task-context state without allocating a callback closure.
    pub fn open_rising_with_context(
        path: &str,
        context: F,
        on_cleanup_failure: fn(&F),
    ) -> Result<Self, Error> {
        let file = OpenOptions::new().read(true).open(path).map_err(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                Error::Unsupported
            } else {
                Error::Io
            }
        })?;
        ffi::irq_check(file.as_raw_fd()).map_err(|_| Error::Unsupported)?;
        let mut resource = Self {
            file,
            registered: true,
            context,
            on_cleanup_failure,
        };
        if ffi::irq_register(resource.file.as_raw_fd()).is_err() {
            // NuttX increments register_count before attach/enable may fail.
            let _ = resource.unregister();
            return Err(Error::Io);
        }
        Ok(resource)
    }

    /// Borrow the notification endpoint for a cancellable task-context wait.
    pub fn file(&self) -> &File {
        &self.file
    }

    pub fn read(&self) -> Result<bool, Error> {
        ffi::irq_read(self.file.as_raw_fd()).map_err(|_| Error::Io)
    }

    /// Attempt unregister exactly once. Repeating a failed unregister could
    /// corrupt NuttX's registration count; the cleanup hook retains the fault.
    pub fn unregister(&mut self) -> Result<(), Error> {
        if !core::mem::take(&mut self.registered) {
            return Ok(());
        }
        ffi::irq_unregister(self.file.as_raw_fd()).map_err(|_| {
            (self.on_cleanup_failure)(&self.context);
            Error::Io
        })
    }
}

impl<F> embedded_hal::digital::ErrorType for Interrupt<F> {
    type Error = Error;
}

impl<F> embedded_hal::digital::InputPin for Interrupt<F> {
    fn is_high(&mut self) -> Result<bool, Error> {
        self.read()
    }
    fn is_low(&mut self) -> Result<bool, Error> {
        self.read().map(|high| !high)
    }
}

impl<F> Drop for Interrupt<F> {
    fn drop(&mut self) {
        let _ = self.unregister();
    }
}
