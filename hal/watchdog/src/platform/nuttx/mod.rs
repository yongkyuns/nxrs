use crate::{ArmedWatchdog, DeviceError};
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, Ordering};

#[allow(unsafe_code)]
mod ffi;

static TAKEN: AtomicBool = AtomicBool::new(false);
struct Lease;
impl Drop for Lease {
    fn drop(&mut self) {
        TAKEN.store(false, Ordering::Release);
    }
}

struct Owner {
    // Close before releasing admission. NuttX close does not stop the timer.
    file: std::fs::File,
    disarmed: bool,
    fault: Option<DeviceError>,
    _lease: Lease,
}

/// Acquire the product's inherited watchdog device. Status reads neither feed
/// nor restart it. Inherited driver timeout accounting may be zero; only the
/// active/reset flags establish adoption, not a software time-left estimate.
pub fn adopt() -> Result<impl ArmedWatchdog, DeviceError> {
    // The selected ESP32-S3 board registers its reset watchdog at this node.
    adopt_at(std::path::Path::new("/dev/watchdog2"))
}

pub(super) fn adopt_at(device: &std::path::Path) -> Result<impl ArmedWatchdog, DeviceError> {
    if TAKEN.swap(true, Ordering::AcqRel) {
        return Err(DeviceError::Busy);
    }
    let lease = Lease;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(device)
        .map_err(|_| DeviceError::Io)?;
    if ffi::status(file.as_raw_fd())? != (true, true) {
        return Err(DeviceError::InvalidState);
    }
    Ok(Owner {
        file,
        disarmed: false,
        fault: None,
        _lease: lease,
    })
}

impl ArmedWatchdog for Owner {
    fn disarm(&mut self) -> Result<(), DeviceError> {
        if let Some(error) = self.fault {
            return Err(error);
        }
        if self.disarmed {
            return Ok(());
        }
        let result = ffi::disarm(self.file.as_raw_fd()).and_then(|()| {
            if ffi::status(self.file.as_raw_fd())?.0 {
                Err(DeviceError::InvalidState)
            } else {
                Ok(())
            }
        });
        self.fault = result.err();
        result?;
        self.disarmed = true;
        Ok(())
    }
}
