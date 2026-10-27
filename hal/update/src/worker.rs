//! One bounded worker owns native flash and image buffers through synchronous I/O.
#![forbid(unsafe_code)]
use crate::{protocol, Error, Phase, Policy, RequestId, StagedImage};
use std::boxed::Box;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc::Receiver,
    Arc, Mutex,
};
use std::time::Instant;

pub(crate) const BUFFER: usize = 4096;
pub(crate) trait Flash: Send + 'static {
    fn validate(&mut self, policy: &Policy) -> Result<(), Error>;
    fn read_primary(&mut self, offset: u32, bytes: &mut [u8]) -> Result<(), Error>;
    fn read_secondary(&mut self, offset: u32, bytes: &mut [u8]) -> Result<(), Error>;
    fn read_scratch(&mut self, offset: u32, bytes: &mut [u8]) -> Result<(), Error>;
    fn erase_secondary(&mut self, block: u32) -> Result<(), Error>;
    fn write_secondary(&mut self, offset: u32, bytes: &[u8]) -> Result<(), Error>;
    /// On error native ownership is unproven and must be retained, not dropped.
    fn close(&mut self) -> Result<(), Error>;
}
pub(crate) trait Verifier: Send + 'static {
    fn authenticate(&mut self, bytes: &[u8], policy: &Policy) -> Result<StagedImage, Error>;
}
pub(crate) struct CryptoVerifier<C>(pub C);
impl<C: firmware_image::Crypto + Send + 'static> Verifier for CryptoVerifier<C> {
    fn authenticate(&mut self, bytes: &[u8], policy: &Policy) -> Result<StagedImage, Error> {
        let image = firmware_image::authenticate(
            bytes,
            &policy.trust(),
            policy.minimum_counter,
            &mut self.0,
        )
        .map_err(|error| match error {
            firmware_image::Error::Format(firmware_image::FormatError::Policy) => Error::Policy,
            _ => Error::Authentication,
        })?;
        Ok(StagedImage {
            security_counter: image.security_counter(),
            version: image.version(),
            payload_sha256: image.payload_sha256(),
        })
    }
}
pub(crate) struct Job {
    pub id: RequestId,
    pub image: Box<[u8]>,
    pub deadline: Instant,
}
pub(crate) struct Inner {
    pub phase: Phase,
    pub progress: u32,
    pub completion: Option<(RequestId, Result<StagedImage, Error>)>,
    pub retirement: Option<Result<(), Error>>,
}
pub(crate) struct Shared {
    pub inner: Mutex<Inner>,
    pub cancel: AtomicBool,
    pub stop: AtomicBool,
    pub wake: Box<dyn Fn() + Send + Sync>,
}
impl Shared {
    pub fn progress(&self, phase: Phase, progress: u32) {
        let mut state = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        state.phase = phase;
        state.progress = progress;
        drop(state);
        (self.wake)();
    }
    fn check(&self, deadline: Instant) -> Result<(), Error> {
        if self.cancel.load(Ordering::Acquire) || self.stop.load(Ordering::Acquire) {
            return Err(Error::Cancelled);
        }
        if Instant::now() >= deadline {
            return Err(Error::TimedOut);
        }
        Ok(())
    }
}

fn stage<R: Flash, V: Verifier>(
    flash: &mut R,
    verifier: &mut V,
    policy: &Policy,
    job: &Job,
    shared: &Shared,
) -> Result<StagedImage, Error> {
    shared.check(job.deadline)?;
    shared.progress(Phase::Authenticating, 0);
    let authenticated = verifier.authenticate(&job.image, policy)?;
    shared.check(job.deadline)?;
    flash.validate(policy)?;
    shared.progress(Phase::Checking, 0);
    let slot = policy.layout.slot_bytes;
    let erase = policy.layout.erase_bytes;
    let align = usize::from(policy.layout.write_align);
    let mut tail = [0; protocol::TAIL];
    flash.read_primary(slot - protocol::TAIL as u32, &mut tail)?;
    if !protocol::idle_primary(&tail, slot) {
        return Err(Error::BootBusy);
    }
    flash.read_secondary(slot - protocol::TAIL as u32, &mut tail)?;
    protocol::idle_secondary(&tail)?;
    flash.read_scratch(
        policy.layout.scratch_bytes - protocol::TAIL as u32,
        &mut tail,
    )?;
    protocol::idle_secondary(&tail)?;
    shared.check(job.deadline)?;
    // Revoke any stale secondary selection before touching image data. The
    // admission above refuses existing control state; staging never writes it.
    let blocks = slot / erase;
    for (index, block) in core::iter::once(blocks - 1)
        .chain(0..blocks - 1)
        .enumerate()
    {
        shared.check(job.deadline)?;
        flash.erase_secondary(block)?;
        shared.progress(Phase::Erasing, (index as u32 + 1) * erase);
    }
    let mut buffer = [255u8; BUFFER];
    for (index, bytes) in job.image.chunks(BUFFER).enumerate() {
        shared.check(job.deadline)?;
        buffer.fill(255);
        buffer[..bytes.len()].copy_from_slice(bytes);
        let padded = bytes.len().div_ceil(align) * align;
        let offset = (index * BUFFER) as u32;
        flash.write_secondary(offset, &buffer[..padded])?;
        shared.progress(Phase::Writing, offset + bytes.len() as u32);
    }
    for (index, bytes) in job.image.chunks(BUFFER).enumerate() {
        shared.check(job.deadline)?;
        let offset = (index * BUFFER) as u32;
        flash.read_secondary(offset, &mut buffer[..bytes.len()])?;
        if bytes != &buffer[..bytes.len()] {
            return Err(Error::Readback);
        }
        shared.progress(Phase::Verifying, offset + bytes.len() as u32);
    }
    // Verify the entire unwritten region, including status/trailer and padding.
    let mut offset = job.image.len() as u32;
    while offset < slot {
        shared.check(job.deadline)?;
        let count = (slot - offset).min(BUFFER as u32) as usize;
        flash.read_secondary(offset, &mut buffer[..count])?;
        if buffer[..count].iter().any(|byte| *byte != 255) {
            return Err(Error::Readback);
        }
        offset += count as u32;
    }
    shared.check(job.deadline)?;
    Ok(authenticated)
}

pub(crate) fn run<R: Flash, V: Verifier>(
    mut flash: R,
    mut verifier: V,
    policy: Policy,
    commands: Receiver<Job>,
    shared: Arc<Shared>,
) {
    while let Ok(job) = commands.recv() {
        let result = catch_unwind(AssertUnwindSafe(|| {
            stage(&mut flash, &mut verifier, &policy, &job, &shared)
        }))
        .unwrap_or(Err(Error::Worker));
        let id = job.id;
        let deadline = job.deadline;
        // Native calls are finished and the owned immutable image is freed
        // before publication. No C callback retains this image or I/O buffer.
        drop(job);
        let mut state = shared
            .inner
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let result = if result.is_ok() {
            shared.check(deadline).map(|()| result.unwrap())
        } else {
            result
        };
        state.phase = if result.is_ok() {
            Phase::Staged
        } else {
            Phase::Idle
        };
        state.completion = Some((id, result));
        drop(state);
        (shared.wake)();
        if shared.stop.load(Ordering::Acquire) {
            break;
        }
    }
    shared.progress(Phase::Closing, 0);
    let retired = catch_unwind(AssertUnwindSafe(|| flash.close())).unwrap_or(Err(Error::Worker));
    if retired.is_err() {
        // A failed native close cannot justify releasing a handle or its lease.
        std::mem::forget(flash);
    }
    let mut state = shared
        .inner
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    state.phase = if retired.is_ok() {
        Phase::Closed
    } else {
        Phase::Closing
    };
    state.retirement = Some(retired);
    drop(state);
    (shared.wake)();
}
