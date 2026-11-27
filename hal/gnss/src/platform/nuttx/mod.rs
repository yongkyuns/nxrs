//! NuttX UART resource ownership. Receiver protocol stays in ordinary Rust.
use crate::session;
#[cfg(test)]
use crate::test_support::{events, ffi};
use crate::{receiver::Receiver, DeviceError, Event, Session};
#[cfg(not(test))]
use acquisition::nuttx as events;
use acquisition::readiness;
use core::sync::atomic::{AtomicU8, Ordering};
use pps_line::PpsLine;
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
mod pps_line;

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
    pps: Option<PpsLine>,
    cleaned: bool,
}
impl<R: Protocol + AsRef<Serial> + AsMut<Serial>> Active<R> {
    fn cleanup(&mut self) -> Result<(), DeviceError> {
        if self.cleaned {
            return Ok(());
        }
        self.cleaned = true;
        let pps = self.pps.as_mut().map_or(Ok(()), |line| {
            line.unregister().map_err(|_| DeviceError::Io)
        });
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
        pps.and(receiver).and(port)
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
    pps_path: Option<&str>,
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
    let mut pps = pps_path
        .map(|path| {
            PpsLine::open_rising_with_context(path, state, |state| {
                state.store(2, Ordering::Release)
            })
            .map_err(|_| DeviceError::Io)
        })
        .transpose()?;
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
    let configured = source.configure(deadline()).and_then(|()| {
        if pps.is_some() {
            source.enable_time_pulse(deadline())?;
        }
        Ok(())
    });
    if let Err(error) = configured {
        if source.restore(deadline()).is_err() {
            state.store(2, Ordering::Release);
        }
        if let Some(line) = &mut pps {
            let _ = line.unregister();
        }
        return Err(crate::receiver::map_error(error));
    }
    Ok(Active {
        receiver: Receiver::new(source),
        pps,
        cleaned: false,
    })
}

/// One canonical physical receiver. Aliases must return this same static object.
pub struct Instance<R: Protocol + AsRef<Serial> + AsMut<Serial> + Send + 'static> {
    id: &'static str,
    path: &'static str,
    pps_path: Option<&'static str>,
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
            pps_path: None,
            baud,
            stack_bytes,
            state: AtomicU8::new(0),
            storage: Mutex::new(None),
            construct,
        }
    }
    #[cfg(feature = "pps")]
    pub const fn with_pps(mut self, path: &'static str) -> Self {
        self.pps_path = Some(path);
        self
    }
}
struct Retained<R: Protocol + AsRef<Serial> + AsMut<Serial> + Send + 'static>(&'static Instance<R>);
impl<R: Protocol + AsRef<Serial> + AsMut<Serial> + Send + 'static> Drop for Retained<R> {
    fn drop(&mut self) {
        let mut storage = self.0.storage.lock().unwrap_or_else(|error| {
            self.0.state.store(2, Ordering::Release);
            error.into_inner()
        });
        *storage = None;
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
        let device = acquire(
            &self.state,
            self.path,
            self.baud,
            self.pps_path,
            self.construct,
        )?;
        *self.storage.lock().map_err(|_| {
            self.state.store(2, Ordering::Release);
            DeviceError::Io
        })? = Some(device);
        // Keep ownership through terminal delivery, sink destruction and cleanup.
        let delivery = (sink, Retained(self));
        let cancel = readiness::Cancel::new(events::pipe).map_err(|_| DeviceError::Io)?;
        let wake = cancel.reader.clone();
        session::start(self.stack_bytes, cancel, delivery, move |stop, emit| {
            let trace = acquisition::probe::Trace::for_source("gnss", self.id);
            let mut storage = self.storage.lock().map_err(|_| DeviceError::Io)?;
            let device = storage.as_mut().ok_or(DeviceError::Io)?;
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
                    trace.wake(None);
                    if let Some(line) = &device.pps {
                        if let Some((sequence, time)) =
                            line.take_timestamp().map_err(|_| DeviceError::Io)?
                        {
                            device.receiver.observe_edge(sequence, time);
                        }
                    }
                    let now = acquisition::monotonic_us();
                    let read = trace.scope("read_parse_deliver", || device.receiver.read_events(now, |event| {
                        if connected {
                            connected = match event {
                                Ok(event) => {
                                    // Count decoded fix reports independently of UART batches.
                                    // This precedes delivery; it does not assert sink acceptance.
                                    if matches!(&event, Event::Fix(_)) {
                                        trace.instant("fix_received");
                                    }
                                    emit(event)
                                },
                                Err(DeviceError::Busy) => true,
                                Err(_) => emit(Event::InvalidFrame),
                            };
                        }
                    }));
                    trace.drain(read.is_ok() && connected);
                    read?;
                    if !connected {
                        break;
                    }
                }
                Ok(())
            })();
            device.cleanup().and(result)
        })
        .map_err(|_| DeviceError::Io)
    }
}
