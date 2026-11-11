//! NuttX UART resource ownership. Receiver protocol stays in ordinary Rust.
#[cfg(test)]
use crate::test_support::{events, ffi};
use crate::{receiver::Receiver, DeviceError, Event, Session};
#[cfg(not(test))]
use acquisition::nuttx as events;
use acquisition::readiness;
use acquisition::Worker;
use core::sync::atomic::{AtomicU8, Ordering};
use peripheral::gnss::Receiver as Protocol;
use std::sync::Mutex;
use std::{
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
};
#[cfg(not(test))]
#[allow(unsafe_code)]
mod ffi;

const TRANSACTION_BUDGET: std::time::Duration = std::time::Duration::from_secs(1);
fn deadline() -> std::time::Instant {
    std::time::Instant::now() + TRANSACTION_BUDGET
}

struct UseGuard(&'static AtomicU8);
impl Drop for UseGuard {
    fn drop(&mut self) {
        let _ = self
            .0
            .compare_exchange(1, 0, Ordering::AcqRel, Ordering::Acquire);
    }
}
struct Port {
    file: File,
    saved: [u8; 64],
    restored: bool,
    state: &'static AtomicU8,
}
impl Port {
    fn restore(&mut self) -> Result<(), DeviceError> {
        if self.restored {
            return Ok(());
        }
        self.restored = true;
        ffi::restore(self.file.as_raw_fd(), &self.saved).map_err(|_| {
            self.state.store(2, Ordering::Release);
            DeviceError::Io
        })
    }
}
impl Drop for Port {
    fn drop(&mut self) {
        let _ = self.restore();
    }
}
/// Owned UART stream supplied to a protocol driver by catalogue construction.
pub struct Serial {
    port: Port,
    _guard: UseGuard,
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
struct Active<R: Protocol + AsRef<Serial> + AsMut<Serial>> {
    receiver: Receiver<R>,
    cleaned: bool,
}
impl<R: Protocol + AsRef<Serial> + AsMut<Serial>> Active<R> {
    fn cleanup(&mut self) -> Result<(), DeviceError> {
        if self.cleaned {
            return Ok(());
        }
        self.cleaned = true;
        let receiver = self
            .receiver
            .source
            .restore(deadline())
            .map_err(crate::receiver::map_error);
        let port = self.receiver.source.as_mut().port.restore();
        if receiver.is_err() || port.is_err() {
            self.receiver
                .source
                .as_ref()
                .port
                .state
                .store(2, Ordering::Release);
        }
        receiver.and(port)
    }
}
impl<R: Protocol + AsRef<Serial> + AsMut<Serial>> Drop for Active<R> {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}
fn acquire<R: Protocol + AsRef<Serial> + AsMut<Serial>>(
    state: &'static AtomicU8,
    path: &str,
    baud: u32,
    construct: fn(Serial) -> R,
) -> Result<Active<R>, DeviceError> {
    state
        .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
        .map_err(|value| {
            if value == 1 {
                DeviceError::Busy
            } else {
                DeviceError::Io
            }
        })?;
    let guard = UseGuard(state);
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
        state,
    };
    ffi::configure(port.file.as_raw_fd(), &port.saved, baud).map_err(|_| DeviceError::Io)?;
    let mut source = construct(Serial {
        port,
        _guard: guard,
    });
    if let Err(error) = source.configure(deadline()) {
        if source.restore(deadline()).is_err() {
            state.store(2, Ordering::Release);
        }
        return Err(crate::receiver::map_error(error));
    }
    Ok(Active {
        receiver: Receiver::new(source),
        cleaned: false,
    })
}

/// One canonical physical receiver. Aliases must return this same static object.
pub struct Instance<R: Protocol + AsRef<Serial> + AsMut<Serial> + Send + 'static> {
    id: &'static str,
    path: &'static str,
    baud: u32,
    stack_bytes: usize,
    state: AtomicU8,
    storage: Mutex<Option<Active<R>>>,
    construct: fn(Serial) -> R,
}
impl<R: Protocol + AsRef<Serial> + AsMut<Serial> + Send + 'static> Instance<R> {
    pub const fn new(
        id: &'static str,
        path: &'static str,
        baud: u32,
        stack_bytes: usize,
        construct: fn(Serial) -> R,
    ) -> Self {
        Self {
            id,
            path,
            baud,
            stack_bytes,
            state: AtomicU8::new(0),
            storage: Mutex::new(None),
            construct,
        }
    }
}
struct Retained<R: Protocol + AsRef<Serial> + AsMut<Serial> + Send + 'static>(&'static Instance<R>);
impl<R: Protocol + AsRef<Serial> + AsMut<Serial> + Send + 'static> Drop for Retained<R> {
    fn drop(&mut self) {
        let mut storage = self.0.storage.lock().unwrap_or_else(|error| {
            self.0.state.store(2, Ordering::Release);
            error.into_inner()
        });
        drop(storage.take());
    }
}
impl<R: Protocol + AsRef<Serial> + AsMut<Serial> + Send + 'static> crate::api::Sealed
    for Instance<R>
{
}
impl<R: Protocol + AsRef<Serial> + AsMut<Serial> + Send + 'static> crate::Device for Instance<R> {
    fn id(&self) -> &'static str {
        self.id
    }
    fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        &'static self,
        sink: S,
    ) -> Result<impl Session + Send + 'static, DeviceError> {
        let device = acquire(&self.state, self.path, self.baud, self.construct)?;
        *self.storage.lock().map_err(|_| {
            self.state.store(2, Ordering::Release);
            DeviceError::Io
        })? = Some(device);
        // Keep ownership through terminal delivery, sink destruction and cleanup.
        let delivery = (sink, Retained(self));
        let cancel = readiness::Cancel::new(events::pipe).map_err(|_| DeviceError::Io)?;
        let wake = cancel.reader.clone();
        Worker::start_guarded(
            "gnss-acquire",
            self.stack_bytes,
            cancel,
            delivery.0,
            delivery.1,
            Event::Failed,
            move |stop, emit| {
                let mut storage = self.storage.lock().map_err(|_| DeviceError::Io)?;
                let device = storage.as_mut().ok_or(DeviceError::Io)?;
                let origin = std::time::Instant::now();
                let result = (|| {
                    while !stop.load(Ordering::Acquire) {
                        if !readiness::wait(
                            &device.receiver.source.as_ref().port.file,
                            &wake,
                            events::wait,
                        )
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
        .map_err(|_| DeviceError::Io)
    }
}
