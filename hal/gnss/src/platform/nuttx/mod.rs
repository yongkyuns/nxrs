//! NuttX UART resource ownership. Receiver protocol stays in ordinary Rust.
use crate::{session::Session, DeviceError, Event, Gnss};
use acquisition::readiness;
use core::sync::atomic::{AtomicU8, Ordering};
use std::{
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
};
#[allow(unsafe_code)]
mod ffi;

include!(concat!(env!("OUT_DIR"), "/gnss_config.rs"));

static OWNED: AtomicU8 = AtomicU8::new(0);
struct Lease;
impl Drop for Lease {
    fn drop(&mut self) {
        let _ = OWNED.compare_exchange(1, 0, Ordering::AcqRel, Ordering::Acquire);
    }
}
struct Port {
    file: File,
    saved: [u8; 64],
    restored: bool,
    // Released after restoring the UART and closing its descriptor.
    _lease: Lease,
}
impl Port {
    fn restore(&mut self) -> Result<(), DeviceError> {
        if self.restored {
            return Ok(());
        }
        self.restored = true;
        ffi::restore(self.file.as_raw_fd(), &self.saved).map_err(|_| {
            OWNED.store(2, Ordering::Release);
            DeviceError::Io
        })
    }
}
impl Drop for Port {
    fn drop(&mut self) {
        let _ = self.restore();
    }
}
impl Read for Port {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.file.read(bytes)
    }
}
impl Write for Port {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.file.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}
struct Device {
    receiver: crate::receiver::Receiver<Port>,
    cleaned: bool,
}
impl Device {
    fn cleanup(&mut self) -> Result<(), DeviceError> {
        if self.cleaned {
            return Ok(());
        }
        self.cleaned = true;
        let receiver = self.receiver.restore();
        let port = self.receiver.io_mut().restore();
        if receiver.is_err() || port.is_err() {
            OWNED.store(2, Ordering::Release);
        }
        receiver.and(port)
    }
}
impl Drop for Device {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}
fn acquire() -> Result<Device, DeviceError> {
    OWNED
        .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
        .map_err(|state| {
            if state == 1 {
                DeviceError::Busy
            } else {
                DeviceError::Io
            }
        })?;
    let lease = Lease;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(ffi::nonblocking_flag())
        .open(DEVICE_PATH)
        .map_err(|_| DeviceError::Io)?;
    let mut saved = [0; 64];
    ffi::capture(file.as_raw_fd(), &mut saved).map_err(|_| DeviceError::Io)?;
    let port = Port {
        file,
        saved,
        restored: false,
        _lease: lease,
    };
    ffi::configure(port.file.as_raw_fd(), &port.saved, BAUD).map_err(|_| DeviceError::Io)?;
    let mut receiver = crate::receiver::Receiver::new(port);
    if let Err(error) = receiver.configure() {
        if receiver.restore().is_err() {
            OWNED.store(2, Ordering::Release);
        }
        return Err(error);
    }
    Ok(Device {
        receiver,
        cleaned: false,
    })
}

pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Gnss + Send + 'static, DeviceError> {
    let mut device = acquire()?;
    let cancel = readiness::Cancel::new(ffi::event_pipe).map_err(|_| DeviceError::Io)?;
    let wake = cancel.reader.clone();
    Session::start(STACK_BYTES, cancel, sink, move |stop, emit| {
        let origin = std::time::Instant::now();
        let result = (|| {
            while !stop.load(Ordering::Acquire) {
                if !readiness::wait(&device.receiver.io().file, &wake, ffi::wait)
                    .map_err(|_| DeviceError::Io)?
                {
                    break;
                }
                let mut connected = true;
                let now = origin.elapsed().as_millis().try_into().unwrap_or(u64::MAX);
                device.receiver.read_events(now, |fix| {
                    if connected {
                        connected = match fix {
                            Ok(fix) => emit(Event::Fix(fix)),
                            Err(DeviceError::Busy) => true,
                            Err(_) => emit(Event::InvalidFrame),
                        };
                    }
                })?;
                if !connected {
                    break;
                }
            }
            Ok(())
        })();
        device.cleanup().and(result)
    })
}
