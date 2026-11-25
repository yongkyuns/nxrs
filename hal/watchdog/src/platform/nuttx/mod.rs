use crate::{ArmedWatchdog, DeviceError, RuntimeWatchdog};
use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

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
    file: File,
    disarmed: bool,
    fault: Option<DeviceError>,
    _lease: Lease,
}

/// Acquire the product's inherited watchdog device. Status reads neither feed
/// nor restart it. Inherited driver timeout accounting may be zero; only the
/// active/reset flags establish adoption, not a software time-left estimate.
pub fn adopt() -> Result<impl ArmedWatchdog, DeviceError> {
    adopt_at(configured_device("inherited")?)
}

/// Start the configured runtime watchdog with a nonzero, whole-millisecond
/// timeout accepted by the device. An already-running timer is left alone.
/// If start succeeds and verification fails, the timer may still reset the system.
pub fn arm(timeout: Duration) -> Result<impl RuntimeWatchdog, DeviceError> {
    arm_at(configured_device("runtime")?, timeout)
}

fn configured_device(mode: &str) -> Result<&'static Path, DeviceError> {
    if option_env!("WATCHDOG_MODE") != Some(mode) {
        return Err(DeviceError::Unsupported);
    }
    option_env!("WATCHDOG_DEVICE")
        .map(Path::new)
        .ok_or(DeviceError::Unsupported)
}

pub(super) fn adopt_at(device: &Path) -> Result<impl ArmedWatchdog, DeviceError> {
    Owner::open(device, true)
}

pub(super) fn arm_at(
    device: &Path,
    timeout: Duration,
) -> Result<impl RuntimeWatchdog, DeviceError> {
    let ms = u32::try_from(timeout.as_millis()).map_err(|_| DeviceError::InvalidTimeout)?;
    if ms == 0 || !timeout.subsec_nanos().is_multiple_of(1_000_000) {
        return Err(DeviceError::InvalidTimeout);
    }
    let owner = Owner::open(device, false)?;
    let fd = owner.file.as_raw_fd();
    ffi::set_timeout(fd, ms)?;
    ffi::start(fd)?;
    if ffi::status(fd)? != (true, true) {
        return Err(DeviceError::InvalidState);
    }
    Ok(owner)
}

impl Owner {
    fn open(device: &Path, active: bool) -> Result<Self, DeviceError> {
        if TAKEN.swap(true, Ordering::AcqRel) {
            return Err(DeviceError::Busy);
        }
        let lease = Lease;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(device)
            .map_err(|_| DeviceError::Io)?;
        if ffi::status(file.as_raw_fd())? != (active, true) {
            return Err(DeviceError::InvalidState);
        }
        Ok(Self {
            file,
            disarmed: false,
            fault: None,
            _lease: lease,
        })
    }
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

impl RuntimeWatchdog for Owner {
    fn feed(&mut self) -> Result<(), DeviceError> {
        if let Some(error) = self.fault {
            return Err(error);
        }
        if self.disarmed {
            return Err(DeviceError::InvalidState);
        }
        ffi::feed(self.file.as_raw_fd())
    }
}
