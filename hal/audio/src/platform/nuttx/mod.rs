use crate::session::{decode_slots, Session, Source, BUFFER_COUNT};
use crate::{Capture, DeviceError, Stream, SAMPLES_PER_BLOCK, SAMPLE_RATE_HZ};
use acquisition::nuttx::{self, Readiness};
use acquisition::readiness::Cancel;
use std::ffi::CString;
use std::fs::{File, OpenOptions};
use std::os::fd::{AsRawFd, OwnedFd};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

#[allow(unsafe_code)]
mod ffi;

const STEREO_BYTES: u16 = (SAMPLES_PER_BLOCK * 8) as u16;
static IN_USE: AtomicBool = AtomicBool::new(false);
static QUEUE_NUMBER: AtomicU32 = AtomicU32::new(0);
static RETIRING: Mutex<Option<Device>> = Mutex::new(None);

pub const AVAILABLE: bool = true;

struct Lease;
impl Drop for Lease {
    fn drop(&mut self) {
        IN_USE.store(false, Ordering::Release);
    }
}

struct Device {
    file: File,
    queue: OwnedFd,
    cancel: Option<Arc<File>>,
    buffers: [Option<ffi::Buffer>; BUFFER_COUNT],
    submitted: [bool; BUFFER_COUNT],
    slot: usize,
    reserved: bool,
    subscribed: bool,
    running: bool,
    _lease: Lease,
}

pub fn open() -> Result<impl Capture, DeviceError> {
    open_device(None)
}

pub fn stream(stack_bytes: usize) -> Result<impl Stream, DeviceError> {
    let cancel = Cancel::new(nuttx::pipe).map_err(|_| DeviceError::Io)?;
    let reader = cancel.reader.clone();
    crate::worker::Buffered::start(cancel, stack_bytes, move || open_device(Some(reader)))
}

fn open_device(cancel: Option<Arc<File>>) -> Result<Session<Device>, DeviceError> {
    let path = option_env!("AUDIO_DEVICE").ok_or(DeviceError::Unsupported)?;
    let slot = match option_env!("AUDIO_SLOT") {
        Some("left") => 0,
        Some("right") => 1,
        _ => return Err(DeviceError::Unsupported),
    };
    // A previous Drop may have initiated stop without receiving its barrier.
    // Retry retirement before admitting a new owner for the same endpoint.
    {
        let mut retiring = RETIRING.lock().map_err(|_| DeviceError::Io)?;
        if let Some(previous) = retiring.as_mut() {
            previous.retire()?;
            *retiring = None;
        }
    }
    if IN_USE.swap(true, Ordering::AcqRel) {
        return Err(DeviceError::Busy);
    }
    let lease = Lease;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .map_err(|_| DeviceError::Io)?;
    let number = QUEUE_NUMBER.fetch_add(1, Ordering::Relaxed);
    let name = CString::new(format!("/nx-audio-{}-{number}", std::process::id()))
        .map_err(|_| DeviceError::InvalidData)?;
    // One dequeue per submitted buffer and room for completion and I/O error.
    let queue = ffi::queue(&name, (BUFFER_COUNT + 2) as u32)?;
    Session::open(Device {
        file,
        queue,
        cancel,
        buffers: [None, None],
        submitted: [false; BUFFER_COUNT],
        slot,
        reserved: false,
        subscribed: false,
        running: false,
        _lease: lease,
    })
}

impl Source for Device {
    fn start(&mut self) -> Result<(), DeviceError> {
        let fd = self.file.as_raw_fd();
        ffi::reserve(fd)?;
        self.reserved = true;
        ffi::configure(fd, SAMPLE_RATE_HZ)?;
        ffi::subscribe(fd, self.queue.as_raw_fd())?;
        self.subscribed = true;
        for buffer in &mut self.buffers {
            *buffer = Some(ffi::Buffer::allocate(fd, STEREO_BYTES)?);
        }
        ffi::start(fd)?;
        self.running = true;
        Ok(())
    }

    fn submit(&mut self, index: usize) -> Result<(), DeviceError> {
        if self.submitted[index] {
            return Err(DeviceError::InvalidData);
        }
        let buffer = self.buffers[index]
            .as_ref()
            .ok_or(DeviceError::InvalidData)?;
        buffer.submit(self.file.as_raw_fd())?;
        self.submitted[index] = true;
        Ok(())
    }

    fn completed(&mut self, until: Instant) -> Result<usize, DeviceError> {
        let queue = self.queue.as_raw_fd();
        let token = loop {
            let remaining = until.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(DeviceError::Timeout);
            }
            let milliseconds = remaining.as_millis().clamp(1, i32::MAX as u128) as i32;
            match nuttx::poll(
                queue,
                self.cancel.as_ref().map(|fd| fd.as_raw_fd()),
                milliseconds,
            ) {
                Ok(Readiness::Device) => match ffi::receive(queue) {
                    Ok(token) => break token,
                    Err(DeviceError::Busy) => continue,
                    Err(error) => return Err(error),
                },
                Ok(Readiness::Timeout) => continue,
                Ok(Readiness::Cancelled) => return Err(DeviceError::Closed),
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => return Err(DeviceError::Io),
            }
        };
        let index = self
            .buffers
            .iter()
            .position(|buffer| {
                buffer
                    .as_ref()
                    .is_some_and(|buffer| buffer.token() == token)
            })
            .ok_or(DeviceError::InvalidData)?;
        if !self.submitted[index] {
            return Err(DeviceError::InvalidData);
        }
        self.submitted[index] = false;
        Ok(index)
    }

    fn read_samples(
        &self,
        index: usize,
        samples: &mut [i16; SAMPLES_PER_BLOCK],
    ) -> Result<(), DeviceError> {
        if self.submitted[index] {
            return Err(DeviceError::InvalidData);
        }
        let buffer = self.buffers[index]
            .as_ref()
            .ok_or(DeviceError::InvalidData)?;
        decode_slots(buffer.samples(STEREO_BYTES)?, self.slot, samples)
    }

    fn retire(&mut self) -> Result<(), DeviceError> {
        let fd = self.file.as_raw_fd();
        if self.running {
            ffi::stop(fd)?;
            self.running = false;
        }
        if self.subscribed {
            ffi::unsubscribe(fd)?;
            self.subscribed = false;
        }
        for buffer in &mut self.buffers {
            if let Some(owned) = buffer.as_ref() {
                owned.free(fd)?;
                *buffer = None;
            }
        }
        if self.reserved {
            ffi::release(fd)?;
            self.reserved = false;
        }
        Ok(())
    }

    fn retain_for_retry(self) {
        let mut retiring = RETIRING.lock().unwrap_or_else(|error| error.into_inner());
        // The retained Device still holds the only admission. Either the
        // slot is empty or no further Device can have been acquired.
        assert!(retiring.is_none());
        *retiring = Some(self);
    }
}
