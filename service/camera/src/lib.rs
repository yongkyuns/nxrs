//! Camera capture and frame history service.
#![no_std]
#![forbid(unsafe_code)]

use camera::{Camera, Capture, DeviceError, Format, Frame};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Device(DeviceError),
    InvalidCapacity,
    InvalidFormat,
    InvalidFrame,
    ClockWentBackwards,
    AlreadyRunning,
    NotRunning,
    SequenceExhausted,
}

impl From<DeviceError> for Error {
    fn from(value: DeviceError) -> Self {
        Self::Device(value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CameraState {
    Stopped,
    Running,
    StopPending,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CaptureProgress {
    Pending,
    Published(u64),
}

/// Read-only capability: applications cannot configure or stop the camera.
/// Cursors belong to one logical stream. Reconnect a consumer to a different
/// stream only by stopping and starting it. Source restarts do not reset IDs.
pub trait Frames {
    fn latest_sequence(&self) -> u64;
    fn next_after(&self, sequence: u64) -> Option<Frame<'_>>;
}

struct Slot<B> {
    metadata: Option<(u64, Capture)>,
    bytes: B,
}

/// Fixed history plus one private staging buffer. Payload capacity is
/// (HISTORY + 1) * BYTES; each buffer must have exactly BYTES bytes.
///
/// Default backing is inline arrays for small products. with_buffers accepts
/// externally placed mutable slices or native-owned buffers through standard
/// AsRef/AsMut traits. Allocation and memory placement remain a platform choice;
/// the service itself never allocates. Native large pools must not be built as
/// inline arrays and subsequently boxed: that still permits stack temporaries.
///
/// Oldest frames are overwritten; consumers report sequence gaps independently.
/// Borrowed frames cannot survive a mutable capture/stop call. No global device,
/// per-reader queue, hidden thread, or runtime dependency is introduced.
pub struct CameraService<D, const BYTES: usize, const HISTORY: usize, B = [u8; BYTES]> {
    device: D,
    state: CameraState,
    format: Option<Format>,
    slots: [Slot<B>; HISTORY],
    staging: B,
    write_index: usize,
    sequence: u64,
    last_poll_ms: Option<u64>,
    last_frame_ms: Option<u64>,
}

impl<D: Camera, const BYTES: usize, const HISTORY: usize> CameraService<D, BYTES, HISTORY> {
    pub fn new(device: D) -> Result<Self, Error> {
        Self::with_buffers(device, core::array::from_fn(|_| [0; BYTES]), [0; BYTES])
    }
}

impl<D: Camera, const BYTES: usize, const HISTORY: usize, B: AsRef<[u8]> + AsMut<[u8]>>
    CameraService<D, BYTES, HISTORY, B>
{
    /// Inject storage without copying payloads or choosing an allocator.
    /// B must expose the same bytes through AsRef and AsMut.
    pub fn with_buffers(
        device: D,
        mut history: [B; HISTORY],
        mut staging: B,
    ) -> Result<Self, Error> {
        if BYTES == 0
            || HISTORY == 0
            || staging.as_ref().len() != BYTES
            || staging.as_mut().len() != BYTES
            || history
                .iter_mut()
                .any(|bytes| bytes.as_ref().len() != BYTES || bytes.as_mut().len() != BYTES)
        {
            return Err(Error::InvalidCapacity);
        }
        Ok(Self {
            device,
            state: CameraState::Stopped,
            format: None,
            slots: history.map(|bytes| Slot {
                metadata: None,
                bytes,
            }),
            staging,
            write_index: 0,
            sequence: 0,
            last_poll_ms: None,
            last_frame_ms: None,
        })
    }

    pub fn state(&self) -> CameraState {
        self.state
    }
    pub fn format(&self) -> Option<Format> {
        self.format
    }

    /// Earliest owner-clock millisecond at which the device says another poll
    /// may make useful progress. None means no time-based retry is advertised.
    pub fn next_poll_at_ms(&self) -> Option<u64> {
        if self.state == CameraState::Running {
            self.device.next_poll_at_ms()
        } else {
            None
        }
    }

    /// Owner-side diagnostics only; deliberately absent from Frames.
    pub fn backend(&self) -> &D {
        &self.device
    }

    pub fn start(&mut self, requested: Format) -> Result<Format, Error> {
        if self.state != CameraState::Stopped {
            return Err(Error::AlreadyRunning);
        }
        if !requested.fits(BYTES) {
            return Err(Error::InvalidFormat);
        }
        // A failed start can also fail cleanup. Keep explicit stop reachable.
        self.state = CameraState::StopPending;
        let actual = match self.device.start(requested) {
            Ok(actual) => actual,
            Err(error) => {
                let _ = self.stop();
                return Err(error.into());
            }
        };
        if !actual.fits(BYTES) {
            self.stop()?;
            return Err(Error::InvalidFormat);
        }
        self.format = Some(actual);
        self.state = CameraState::Running;
        Ok(actual)
    }

    pub fn poll(&mut self, now_ms: u64) -> Result<CaptureProgress, Error> {
        if self.state != CameraState::Running {
            return Err(Error::NotRunning);
        }
        if self.last_poll_ms.is_some_and(|previous| now_ms < previous) {
            return Err(Error::ClockWentBackwards);
        }
        self.last_poll_ms = Some(now_ms);
        let next = self
            .sequence
            .checked_add(1)
            .ok_or(Error::SequenceExhausted)?;
        let Some(capture) = self.device.poll_frame(now_ms, self.staging.as_mut())? else {
            return Ok(CaptureProgress::Pending);
        };
        if Some(capture.format) != self.format
            || capture.len > BYTES
            || !capture.format.accepts_len(capture.len)
            || capture.timestamp_ms > now_ms
            || self
                .last_frame_ms
                .is_some_and(|previous| capture.timestamp_ms < previous)
        {
            return Err(Error::InvalidFrame);
        }
        let slot = &mut self.slots[self.write_index];
        core::mem::swap(&mut slot.bytes, &mut self.staging);
        slot.metadata = Some((next, capture));
        self.write_index = (self.write_index + 1) % HISTORY;
        self.sequence = next;
        self.last_frame_ms = Some(capture.timestamp_ms);
        Ok(CaptureProgress::Published(next))
    }

    pub fn stop(&mut self) -> Result<(), Error> {
        if self.state == CameraState::Stopped {
            return Ok(());
        }
        self.state = CameraState::StopPending;
        self.format = None;
        for slot in &mut self.slots {
            slot.metadata = None;
        }
        self.device.stop()?;
        self.state = CameraState::Stopped;
        Ok(())
    }
}

impl<D: Camera, const BYTES: usize, const HISTORY: usize, B: AsRef<[u8]> + AsMut<[u8]>> Frames
    for CameraService<D, BYTES, HISTORY, B>
{
    fn latest_sequence(&self) -> u64 {
        self.sequence
    }

    fn next_after(&self, sequence: u64) -> Option<Frame<'_>> {
        if self.state != CameraState::Running {
            return None;
        }
        let slot = self
            .slots
            .iter()
            .filter(|slot| slot.metadata.is_some_and(|(number, _)| number > sequence))
            .min_by_key(|slot| slot.metadata.map(|(number, _)| number))?;
        let (sequence, capture) = slot.metadata?;
        Some(Frame {
            sequence,
            capture,
            bytes: &slot.bytes.as_ref()[..capture.len],
        })
    }
}
