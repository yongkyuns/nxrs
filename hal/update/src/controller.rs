//! Nonblocking admission/cancellation and explicit quiescence/retirement.
#![forbid(unsafe_code)]
use crate::{
    worker::{self, Flash, Inner, Job, Shared, Verifier},
    Error, Phase, Policy, RequestId, Status, Update,
};
use core::num::NonZeroU32;
use std::boxed::Box;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc::{sync_channel, SyncSender, TrySendError},
    Arc, TryLockError,
};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

pub struct Rejected {
    pub error: Error,
    pub image: Box<[u8]>,
}
impl core::fmt::Debug for Rejected {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("Rejected")
            .field("error", &self.error)
            .field("image_bytes", &self.image.len())
            .finish()
    }
}
struct Active {
    id: RequestId,
    deadline: Instant,
    cancel_at: Option<Instant>,
    reported: bool,
    quiet: bool,
}
pub struct Stager {
    commands: Option<SyncSender<Job>>,
    shared: Arc<Shared>,
    thread: Option<JoinHandle<()>>,
    active: Option<Active>,
    next: u32,
    fault: Option<Error>,
    max_image: usize,
    retirement_timeout: Duration,
    shutdown_at: Option<Instant>,
}
impl Stager {
    pub(crate) fn spawn<R: Flash, V: Verifier>(
        flash: R,
        verifier: V,
        policy: Policy,
        stack: usize,
        retirement_timeout: Duration,
        wake: impl Fn() + Send + Sync + 'static,
    ) -> Result<Self, Error> {
        if retirement_timeout.is_zero() || retirement_timeout > Duration::from_secs(300) {
            return Err(Error::Policy);
        }
        let max_image = policy.layout.slot_bytes as usize
            - (3 * usize::from(policy.layout.max_sectors) * usize::from(policy.layout.write_align)
                + 48);
        let shared = Arc::new(Shared {
            inner: std::sync::Mutex::new(Inner {
                phase: Phase::Idle,
                progress: 0,
                completion: None,
                retirement: None,
            }),
            cancel: AtomicBool::new(false),
            stop: AtomicBool::new(false),
            wake: Box::new(wake),
        });
        let (commands, receive) = sync_channel(1);
        let owner = Arc::clone(&shared);
        let thread = std::thread::Builder::new()
            .name("firmware-stage".into())
            .stack_size(stack)
            .spawn(move || worker::run(flash, verifier, policy, receive, owner))
            .map_err(|_| Error::Worker)?;
        Ok(Self {
            commands: Some(commands),
            shared,
            thread: Some(thread),
            active: None,
            next: 1,
            fault: None,
            max_image,
            retirement_timeout,
            shutdown_at: None,
        })
    }
    pub fn stage(&mut self, image: Box<[u8]>, timeout: Duration) -> Result<RequestId, Rejected> {
        let refusal = if self.commands.is_none() {
            Some(Error::Closed)
        } else if let Some(error) = self.fault {
            Some(error)
        } else if self.active.is_some() {
            Some(Error::Busy)
        } else if image.is_empty()
            || image.len() > self.max_image
            || timeout.is_zero()
            || timeout > Duration::from_secs(1800)
        {
            Some(Error::Policy)
        } else if self.next == 0 {
            Some(Error::Exhausted)
        } else {
            None
        };
        if let Some(error) = refusal {
            return Err(Rejected { error, image });
        }
        let Some(deadline) = Instant::now().checked_add(timeout) else {
            return Err(Rejected {
                error: Error::Policy,
                image,
            });
        };
        let id = RequestId(NonZeroU32::new(self.next).expect("checked request ID"));
        self.shared.cancel.store(false, Ordering::Release);
        let job = Job {
            id,
            image,
            deadline,
        };
        match self
            .commands
            .as_ref()
            .expect("checked admission")
            .try_send(job)
        {
            Ok(()) => {
                self.next = self.next.checked_add(1).unwrap_or(0);
                self.active = Some(Active {
                    id,
                    deadline,
                    cancel_at: None,
                    reported: false,
                    quiet: false,
                });
                Ok(id)
            }
            Err(TrySendError::Full(job)) => Err(Rejected {
                error: Error::Busy,
                image: job.image,
            }),
            Err(TrySendError::Disconnected(job)) => {
                self.fault = Some(Error::Worker);
                Err(Rejected {
                    error: Error::Worker,
                    image: job.image,
                })
            }
        }
    }
    /// Request cancellation; the result may already have won the completion race.
    /// A reported outcome is distinct from the later native quiescence barrier.
    pub fn cancel(&mut self, id: RequestId) -> Result<(), Error> {
        let active = self.active.as_mut().ok_or(Error::Busy)?;
        if active.id != id || active.reported {
            return Err(Error::Busy);
        }
        active.cancel_at.get_or_insert_with(Instant::now);
        self.shared.cancel.store(true, Ordering::Release);
        (self.shared.wake)();
        Ok(())
    }
    pub fn poll(&mut self) -> Update {
        if self.active.as_ref().is_some_and(|active| active.quiet) {
            return Update::Quiescent {
                id: self.active.take().unwrap().id,
            };
        }
        let mut state = match self.shared.inner.try_lock() {
            Ok(state) => state,
            Err(TryLockError::WouldBlock) => {
                (self.shared.wake)();
                return Update::None;
            }
            Err(TryLockError::Poisoned(error)) => {
                self.fault.get_or_insert(Error::Worker);
                error.into_inner()
            }
        };
        let Some(active) = self.active.as_mut() else {
            return Update::None;
        };
        if let Some((id, result)) = state.completion.take() {
            if id != active.id {
                self.fault.get_or_insert(Error::Worker);
                return Update::None;
            }
            if let Err(error) = result {
                if !matches!(
                    error,
                    Error::Authentication | Error::BootBusy | Error::Cancelled
                ) {
                    self.fault.get_or_insert(error);
                }
            }
            if active.reported {
                let id = active.id;
                self.active = None;
                return Update::Quiescent { id };
            }
            active.reported = true;
            active.quiet = true;
            return Update::Completed {
                id,
                result,
                quiescent: true,
            };
        }
        let now = Instant::now();
        if !active.reported && (active.cancel_at.is_some() || now >= active.deadline) {
            let error = if active.cancel_at.is_some() {
                Error::Cancelled
            } else {
                self.fault.get_or_insert(Error::TimedOut);
                Error::TimedOut
            };
            active.cancel_at.get_or_insert(now);
            active.reported = true;
            self.shared.cancel.store(true, Ordering::Release);
            return Update::Completed {
                id: active.id,
                result: Err(error),
                quiescent: false,
            };
        }
        if active.reported
            && active
                .cancel_at
                .is_some_and(|start| now.duration_since(start) >= self.retirement_timeout)
        {
            self.fault.get_or_insert(Error::RetirementTimedOut);
        }
        Update::None
    }
    pub fn status(&mut self) -> Status {
        match self.shared.inner.try_lock() {
            Ok(state) => Status {
                phase: state.phase,
                completed_bytes: state.progress,
                fault: self.fault,
                retired: matches!(state.retirement, Some(Ok(()))),
            },
            Err(_) => {
                (self.shared.wake)();
                Status {
                    phase: Phase::Checking,
                    completed_bytes: 0,
                    fault: self.fault,
                    retired: false,
                }
            }
        }
    }
    pub fn begin_shutdown(&mut self) {
        self.shutdown_at.get_or_insert_with(Instant::now);
        self.shared.stop.store(true, Ordering::Release);
        self.shared.cancel.store(true, Ordering::Release);
        self.commands.take();
    }
    /// Never waits for native I/O. A false result retains the worker/resources.
    pub fn poll_shutdown(&mut self) -> Result<bool, Error> {
        self.begin_shutdown();
        let state = match self.shared.inner.try_lock() {
            Ok(state) => state,
            Err(_) => {
                (self.shared.wake)();
                return Ok(false);
            }
        };
        if let Some(result) = state.retirement {
            drop(state);
            if self
                .thread
                .as_ref()
                .is_some_and(|thread| !thread.is_finished())
            {
                return Ok(false);
            }
            if let Some(thread) = self.thread.take() {
                thread.join().map_err(|_| Error::Worker)?;
            }
            if let Err(error) = result {
                self.fault.get_or_insert(error);
                return Err(error);
            }
            return Ok(true);
        }
        if self
            .shutdown_at
            .is_some_and(|start| start.elapsed() >= self.retirement_timeout)
        {
            self.fault.get_or_insert(Error::RetirementTimedOut);
            return Err(Error::RetirementTimedOut);
        }
        Ok(false)
    }
}
impl Drop for Stager {
    fn drop(&mut self) {
        self.begin_shutdown(); /* Detached worker owns every buffer/lease until native return. */
    }
}
