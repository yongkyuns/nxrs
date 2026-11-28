//! One bounded request and its retained result for an active receiver session.

use acquisition::Wake;
use peripheral::gnss::{aiding::Progress, Error, Receiver};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, TryLockError, Weak,
    },
    time::{Duration, Instant},
};

const TRANSACTION_BUDGET: Duration = Duration::from_secs(1);
// Only used when the client is preempted during its short slot publication.
// Sleeping lets that owner release the slot; an immediate retry could starve it.
const SLOT_RETRY: Duration = Duration::from_millis(1);

pub use peripheral::gnss::aiding::{
    Aid, Capabilities, Outcome, PositionAid, Rejection, SubmitError, TimeAid,
};

/// Identity within one aiding handle. A replacement session has a new handle.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RequestId(u64);

/// Reading this retained result releases the handle's one outstanding request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Completion {
    pub id: RequestId,
    pub result: Result<Outcome, SubmitError>,
}

/// A session's exclusive aiding client, obtained once through `Session::aiding`.
///
/// Admission never waits for a busy slot. Requests allocate no memory. The
/// receiver continues acquiring fixes while its existing owner applies the aid.
pub struct AidingHandle {
    shared: Arc<Shared>,
    pending: Option<RequestId>,
    next: Option<u64>,
}

struct Shared {
    capabilities: Capabilities,
    wake: Weak<dyn Wake>,
    slot: Mutex<Slot>,
    accepting: AtomicBool,
}

struct Slot {
    request: Option<Ticket>,
    active: Option<RequestId>,
    completion: Option<Completion>,
}

#[derive(Clone, Copy)]
pub(crate) struct Ticket {
    pub id: RequestId,
    pub aid: Aid,
}

pub(crate) struct Owner {
    shared: Arc<Shared>,
    // A retired client retains its result, never the owner's wake descriptors.
    _wake: Arc<dyn Wake>,
    deadline: Option<Instant>,
    retry: Option<Instant>,
    writable: bool,
    completion: Option<Result<Outcome, SubmitError>>,
}

pub(crate) struct Stop(Arc<Shared>);
impl Stop {
    pub(crate) fn request(&self) {
        self.0.accepting.store(false, Ordering::Release);
    }
}

pub(crate) fn channel(capabilities: Capabilities, wake: Arc<dyn Wake>) -> (AidingHandle, Owner) {
    let shared = Arc::new(Shared {
        capabilities,
        wake: Arc::downgrade(&wake),
        slot: Mutex::new(Slot {
            request: None,
            active: None,
            completion: None,
        }),
        accepting: AtomicBool::new(true),
    });
    (
        AidingHandle {
            shared: shared.clone(),
            pending: None,
            next: Some(1),
        },
        Owner {
            shared,
            _wake: wake,
            deadline: None,
            retry: None,
            writable: false,
            completion: None,
        },
    )
}

impl AidingHandle {
    pub fn capabilities(&self) -> Capabilities {
        self.shared.capabilities
    }

    /// Submit a hint stamped with `gnss::monotonic_us()` in this boot lifetime.
    /// Consume the preceding completion before submitting another hint.
    /// A rejection from this method means that no request was admitted.
    pub fn try_submit(&mut self, aid: Aid) -> Result<RequestId, SubmitError> {
        if self.pending.is_some() {
            return Err(SubmitError::Busy);
        }
        if !self.shared.accepting.load(Ordering::Acquire) {
            return Err(SubmitError::Stopped);
        }
        let supported = match aid {
            Aid::Time(_) => self.shared.capabilities.time,
            Aid::Position(_) => self.shared.capabilities.position,
        };
        if !supported {
            return Err(SubmitError::Unsupported);
        }
        aid.validate(acquisition::monotonic_us())?;
        let id = RequestId(self.next.ok_or(SubmitError::Stopped)?);
        let mut slot = self.shared.slot.try_lock().map_err(|error| match error {
            TryLockError::WouldBlock => SubmitError::Busy,
            TryLockError::Poisoned(_) => SubmitError::Io,
        })?;
        if !self.shared.accepting.load(Ordering::Acquire) {
            return Err(SubmitError::Stopped);
        }
        let wake = self.shared.wake.upgrade().ok_or(SubmitError::Stopped)?;
        debug_assert!(slot.request.is_none() && slot.active.is_none() && slot.completion.is_none());
        slot.request = Some(Ticket { id, aid });
        // The pipe is nonblocking. Keep admission and its wake atomic with
        // respect to the owner, so a failed wake can roll back the request.
        if wake.wake().is_err() {
            slot.request = None;
            return Err(SubmitError::Io);
        }
        self.pending = Some(id);
        self.next = id.0.checked_add(1);
        Ok(id)
    }

    /// Reconcile after `Event::AidingReady`, other wakes, and before waiting.
    /// A notification may be coalesced or rejected; this result remains retained.
    pub fn try_completion(&mut self) -> Option<Completion> {
        let id = self.pending?;
        let mut slot = match self.shared.slot.try_lock() {
            Ok(slot) => slot,
            Err(TryLockError::WouldBlock) => return None,
            Err(TryLockError::Poisoned(error)) => error.into_inner(),
        };
        let completion = slot.completion.take()?;
        debug_assert_eq!(completion.id, id);
        self.pending = None;
        Some(completion)
    }
}

impl Owner {
    pub(crate) fn stop_handle(&self) -> Stop {
        Stop(self.shared.clone())
    }
    pub(crate) fn writable(&self) -> bool {
        self.writable
    }

    pub(crate) fn deadline(&self) -> Option<Instant> {
        match (self.deadline, self.retry) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        }
    }

    /// One bounded protocol step. All serial I/O and delivery occur outside
    /// the slot lock; contention re-arms the owner's existing wait deadline.
    pub(crate) fn advance(
        &mut self,
        source: &mut impl Receiver,
        now: Instant,
        now_us: u64,
    ) -> Result<bool, Error> {
        self.retry = None;
        if self.deadline.is_none() && self.completion.is_none() {
            let ticket = match self.shared.slot.try_lock() {
                Ok(mut slot) => {
                    let ticket = slot.request.take();
                    slot.active = ticket.map(|ticket| ticket.id);
                    ticket
                }
                Err(TryLockError::WouldBlock) => {
                    self.retry = Some(now + SLOT_RETRY);
                    return Ok(false);
                }
                Err(TryLockError::Poisoned(_)) => return Err(Error::Io),
            };
            if let Some(ticket) = ticket {
                let deadline = now + TRANSACTION_BUDGET;
                match source.begin_aiding(ticket.aid, now_us, deadline) {
                    Ok(()) => self.deadline = Some(deadline),
                    Err(error) => self.completion = Some(Err(error)),
                }
            }
        }
        if self.deadline.is_some() {
            match source.advance_aiding(now, now_us) {
                Ok(Progress::Writing) => self.writable = true,
                Ok(Progress::AwaitingAck) => self.writable = false,
                Ok(Progress::Complete(result)) => {
                    self.deadline = None;
                    self.writable = false;
                    self.completion = Some(result);
                }
                Ok(Progress::Idle) => return Err(Error::InvalidData),
                Err(error) => {
                    self.completion = Some(Err(SubmitError::Io));
                    return Err(error);
                }
            }
        }
        let Some(result) = self.completion else {
            return Ok(false);
        };
        match self.shared.slot.try_lock() {
            Ok(mut slot) => {
                if let Some(id) = slot.active.take() {
                    slot.completion = Some(Completion { id, result });
                }
                self.completion = None;
                Ok(true)
            }
            Err(TryLockError::WouldBlock) => {
                self.retry = Some(now + SLOT_RETRY);
                Ok(false)
            }
            Err(TryLockError::Poisoned(_)) => Err(Error::Io),
        }
    }

    /// Joined teardown is outside acquisition dispatch. Preserve a completed
    /// result, and retire any operation which the receiver did not finish.
    pub(crate) fn close(&mut self, result: Result<Outcome, SubmitError>) -> bool {
        self.shared.accepting.store(false, Ordering::Release);
        let result = self.completion.take().unwrap_or(result);
        let mut slot = self
            .shared
            .slot
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let id = slot
            .active
            .take()
            .or_else(|| slot.request.take().map(|ticket| ticket.id));
        if let Some(id) = id {
            slot.completion = Some(Completion { id, result });
        }
        id.is_some()
    }
}

impl Drop for Owner {
    fn drop(&mut self) {
        self.close(Ok(Outcome::Cancelled));
    }
}

#[cfg(test)]
mod tests;
