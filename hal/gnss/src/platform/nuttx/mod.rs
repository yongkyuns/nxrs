//! NuttX UART resource ownership. Receiver protocol stays in ordinary Rust.
use crate::acquisition::Worker;
use crate::{DeviceError, Event, EventSink, Gnss};
#[path = "../../../../../driver/nuttx/readiness.rs"]
mod readiness;
use core::sync::atomic::{AtomicU8, Ordering};
use std::{
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
};
#[allow(unsafe_code)]
mod ffi;

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
struct Serial {
    port: Port,
    _lease: Lease,
}
impl Read for Serial {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.port.file.read(bytes)
    }
}
impl Write for Serial {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.port.file.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.port.file.flush()
    }
}
struct Device {
    receiver: crate::receiver::Receiver<Serial>,
    cleaned: bool,
}
impl Device {
    fn cleanup(&mut self) -> Result<(), DeviceError> {
        if self.cleaned {
            return Ok(());
        }
        self.cleaned = true;
        let receiver = self.receiver.restore();
        let port = self.receiver.io.port.restore();
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
    let path = option_env!("GNSS_DEVICE").ok_or(DeviceError::Unsupported)?;
    let baud = option_env!("GNSS_BAUD")
        .and_then(|b| b.parse::<u32>().ok())
        .ok_or(DeviceError::Unsupported)?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(ffi::nonblocking_flag())
        .open(path)
        .map_err(|_| DeviceError::Io)?;
    let mut saved = [0; 64];
    ffi::capture(file.as_raw_fd(), &mut saved).map_err(|_| DeviceError::Io)?;
    let port = Port {
        file,
        saved,
        restored: false,
    };
    ffi::configure(port.file.as_raw_fd(), &port.saved, baud).map_err(|_| DeviceError::Io)?;
    // Port restores/closes before the lease can admit another receiver.
    let mut receiver = crate::receiver::Receiver::new(Serial {
        port,
        _lease: lease,
    });
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

struct Session(Worker<readiness::Cancel, DeviceError>);
impl Gnss for Session {
    fn request_stop(&mut self) -> Result<(), DeviceError> {
        self.0.request_stop().map_err(|_| DeviceError::Io)
    }
    fn join(&mut self) -> Result<(), DeviceError> {
        self.request_stop()?;
        self.0.join(DeviceError::Io)
    }
    fn is_finished(&self) -> bool {
        self.0.is_finished()
    }
    fn dropped(&self) -> u32 {
        self.0.dropped()
    }
}
pub fn open<S: EventSink<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Gnss + Send + 'static, DeviceError> {
    let mut device = acquire()?;
    let cancel = readiness::Cancel::new().map_err(|_| DeviceError::Io)?;
    let wake = cancel.reader.clone();
    let stack = option_env!("GNSS_STACK_BYTES")
        .and_then(|v| v.parse().ok())
        .ok_or(DeviceError::Unsupported)?;
    Worker::start(
        "gnss-acquire",
        stack,
        cancel,
        sink,
        Event::Failed,
        move |stop, emit| {
            let origin = std::time::Instant::now();
            let result = (|| {
                while !stop.load(Ordering::Acquire) {
                    if !readiness::wait(&device.receiver.io.port.file, &wake)
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
        },
    )
    .map(Session)
    .map_err(|_| DeviceError::Io)
}
