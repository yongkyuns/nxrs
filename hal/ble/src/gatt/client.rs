//! Bounded, single-link GATT client state machine for a service's event loop.
//!
//! This is separate from scanning: scan queues do not carry GATT value buffers.
//! No thread, allocation, clock, retry policy or native stack is created here.
//! A platform driver must satisfy the cancellation/lifetime contract below.
//! The NuttX adapter bypasses legacy unbounded central ioctls.
#![forbid(unsafe_code)]

use crate::{ControllerError, Error, PeerId, Rejected, RequestId, SessionId, Uuid};
use core::num::NonZeroU16;
pub use hal_common::BoundedBytes as Value;
use hal_common::OperationId;

/// A scan-local peer identity must travel with the session that issued it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Peer {
    pub session: SessionId,
    pub id: PeerId,
}

/// One successful connection, identified by its connect request, not an OS handle.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LinkId(RequestId);

impl LinkId {
    pub fn request(self) -> RequestId {
        self.0
    }
}

/// A discovered handle is valid only on the connection that discovered it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Attribute {
    link: LinkId,
    handle: NonZeroU16,
}

impl Attribute {
    pub fn link(self) -> LinkId {
        self.link
    }
    /// For platform adapters; applications normally keep the entire Attribute.
    pub fn handle(self) -> NonZeroU16 {
        self.handle
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    Device(Error),
    Disconnected,
    Cancelled,
    /// A remote ATT error, not a local errno or an HCI disconnect reason.
    Att(u8),
    /// The remote operation was rejected, but the OS omitted its ATT code.
    /// This does not by itself invalidate an established link.
    Remote(Error),
    Protocol,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Command<const N: usize> {
    Connect {
        peer: Peer,
    },
    /// Resolve one characteristic by UUID; no persistent handle cache.
    Discover {
        service: Uuid,
        characteristic: Uuid,
    },
    /// Read a complete bounded value, not an implicitly truncated prefix.
    /// A driver without the required long-read support must reject it.
    /// Long reads can span requests; the peer controls snapshot consistency.
    Read {
        attribute: Attribute,
    },
    /// Acknowledged write, not write-without-response. Never silently truncate.
    Write {
        attribute: Attribute,
        value: Value<N>,
    },
    Disconnect,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Request<const N: usize> {
    pub id: RequestId,
    pub link: Option<LinkId>,
    pub command: Command<N>,
}

/// Normalized native result. Only the client binds discovered handles to a link.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reply<const N: usize> {
    Connected,
    Discovered { handle: NonZeroU16 },
    Read(Value<N>),
    Written,
    Disconnected,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Completion<const N: usize> {
    Connected(LinkId),
    Discovered(Attribute),
    Read(Value<N>),
    Written,
    Disconnected,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventKind<const N: usize> {
    Completed {
        request: RequestId,
        result: Result<Reply<N>, Failure>,
    },
    /// Does not imply callback quiescence. The client requests a drain barrier.
    Disconnected {
        connection: RequestId,
        reason: Option<u8>,
    },
    /// All old callbacks/requests are drained AND the old link is gone.
    Quiescent { recovery: RequestId },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Event<const N: usize> {
    pub session: SessionId,
    pub kind: EventKind<N>,
}

/// Nonblocking platform boundary. Admission Err accepts nothing and returns
/// the owned request. Accepted commands retain their storage in the driver,
/// never pointers into the client's stack. There is at most one operation.
/// If admission is temporarily Busy with no pending request, arrange a wake
/// when another attempt may succeed. The caller retains the command and its
/// original deadline; a Busy rejection is not an operation completion.
/// Connect must reject peers from an expired/unknown scan session rather than
/// resolving a reused numeric PeerId to a different device.
///
/// Completion is terminal for that request, including callbacks already in
/// flight. Disconnect events are NOT such a barrier. recover() must reliably
/// record cancellation/teardown even if the ordinary command/event queue is
/// full. It must not block waiting for callbacks. Quiescent acknowledges the
/// exact recovery token only after all callbacks have stopped and the old link
/// is gone; an accepted disconnect has the same barrier on successful completion.
/// A queued disconnect or cancel acknowledgement alone is insufficient.
///
/// The driver retains terminal events until poll() consumes them, and wakes
/// the owner's existing event source when they become available. No periodic
/// polling or extra service thread is required. Dropping a driver must keep
/// native callback storage alive until quiescence; it must not join/block.
/// Late callbacks may be discarded by identity, never relabelled as new work.
/// Link events are FIFO: connection completion precedes subsequent link loss.
/// Sessions must not be reused when a driver/client is reacquired.
pub trait Driver<const N: usize> {
    fn session(&self) -> SessionId;
    fn submit(&mut self, request: Request<N>) -> Result<(), Rejected<Request<N>, Error>>;
    fn recover(&mut self, token: RequestId);
    fn poll(&mut self) -> Option<Event<N>>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Idle,
    Connecting,
    Connected(LinkId),
    Disconnecting,
    Recovering,
    /// Recovery missed its deadline; native resources may still be retained.
    Failed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Outcome<const N: usize> {
    pub id: RequestId,
    pub result: Result<Completion<N>, Failure>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Update<const N: usize> {
    /// No queued event or newly expired deadline; the owner may wait.
    None,
    /// A stale event was consumed; keep draining before the owner waits.
    Ignored,
    Finished(Outcome<N>),
    /// The link is gone. An explicit Disconnect still awaits its terminal
    /// callback barrier; otherwise recovery starts. Other pending operations
    /// report Finished(Disconnected) instead of a separate link update.
    Disconnected {
        link: LinkId,
        reason: Option<u8>,
    },
    Recovered,
    RecoveryTimedOut,
    /// A late barrier released resources, but does not clear the sticky fault
    /// or re-enable admission after RecoveryTimedOut.
    Released,
}

#[derive(Clone, Copy)]
enum Kind {
    Connect,
    Discover,
    Read,
    Write,
    Disconnect,
}

impl Kind {
    fn of<const N: usize>(command: Command<N>) -> Self {
        match command {
            Command::Connect { .. } => Self::Connect,
            Command::Discover { .. } => Self::Discover,
            Command::Read { .. } => Self::Read,
            Command::Write { .. } => Self::Write,
            Command::Disconnect => Self::Disconnect,
        }
    }
}

struct Pending {
    id: RequestId,
    kind: Kind,
    deadline: u64,
}
struct Recovery {
    id: RequestId,
    deadline: u64,
}

/// Deadlines use the owner's monotonic millisecond clock. Call poll() on its
/// normal wake source or next_deadline_ms(), whichever comes first. A timeout
/// is reported once; no new operation is admitted before the drain barrier.
pub struct Client<D, const N: usize> {
    driver: D,
    session: SessionId,
    next_id: u64,
    link: Option<LinkId>,
    pending: Option<Pending>,
    recovery: Option<Recovery>,
    recovery_timeout_ms: u64,
    recovery_failed: bool,
    fault: Option<Failure>,
    last_now_ms: u64,
}

impl<D: Driver<N>, const N: usize> Client<D, N> {
    pub fn new(driver: D, recovery_timeout_ms: u64) -> Result<Self, ControllerError> {
        if recovery_timeout_ms == 0 {
            return Err(ControllerError::InvalidTimeout);
        }
        Ok(Self {
            session: driver.session(),
            driver,
            next_id: 0,
            link: None,
            pending: None,
            recovery: None,
            recovery_timeout_ms,
            recovery_failed: false,
            fault: None,
            last_now_ms: 0,
        })
    }

    pub fn state(&self) -> State {
        if self.recovery_failed {
            State::Failed
        } else if self.recovery.is_some() {
            State::Recovering
        } else if self
            .pending
            .as_ref()
            .is_some_and(|p| matches!(p.kind, Kind::Disconnect))
        {
            State::Disconnecting
        } else if let Some(link) = self.link {
            State::Connected(link)
        } else if self.pending.is_some() {
            State::Connecting
        } else {
            State::Idle
        }
    }

    pub fn is_busy(&self) -> bool {
        self.pending.is_some() || self.recovery.is_some()
    }
    pub fn fault(&self) -> Option<Failure> {
        self.fault
    }

    pub fn next_deadline_ms(&self) -> Option<u64> {
        self.pending.as_ref().map(|p| p.deadline).or_else(|| {
            self.recovery
                .as_ref()
                .filter(|_| !self.recovery_failed)
                .map(|r| r.deadline)
        })
    }

    pub fn submit(
        &mut self,
        command: Command<N>,
        now_ms: u64,
        timeout_ms: u64,
    ) -> Result<RequestId, Rejected<Command<N>, ControllerError>> {
        let reject = |error| Rejected {
            value: command,
            error,
        };
        self.check_clock(now_ms).map_err(reject)?;
        if self.recovery_failed {
            return Err(reject(ControllerError::Device(Error::Unavailable)));
        }
        if self.is_busy() {
            return Err(reject(ControllerError::Device(Error::Busy)));
        }
        match command {
            Command::Connect { .. } if self.link.is_some() => {
                return Err(reject(ControllerError::Device(Error::Busy)));
            }
            Command::Connect { .. } => (),
            _ if self.link.is_none() => {
                return Err(reject(ControllerError::Device(Error::Unavailable)));
            }
            Command::Read { attribute } | Command::Write { attribute, .. }
                if Some(attribute.link) != self.link =>
            {
                return Err(reject(ControllerError::Device(Error::InvalidValue)));
            }
            _ => (),
        }
        let deadline = now_ms
            .checked_add(timeout_ms)
            .filter(|_| timeout_ms != 0)
            .ok_or_else(|| reject(ControllerError::InvalidTimeout))?;
        // Reserve room for recovery as well; never silently wrap a deadline.
        deadline
            .checked_add(self.recovery_timeout_ms)
            .ok_or_else(|| reject(ControllerError::InvalidTimeout))?;
        let next = self
            .next_id
            .checked_add(1)
            .ok_or_else(|| reject(ControllerError::IdExhausted))?;
        let id = RequestId {
            session: self.session,
            operation: OperationId::new(next).unwrap(),
        };
        self.next_id = next; // Rejected native attempts must not reuse identities.
        self.driver
            .submit(Request {
                id,
                link: self.link,
                command,
            })
            .map_err(|r| Rejected {
                value: r.value.command,
                error: ControllerError::Device(r.error),
            })?;
        self.pending = Some(Pending {
            id,
            kind: Kind::of(command),
            deadline,
        });
        Ok(id)
    }

    pub fn cancel(&mut self, now_ms: u64) -> Result<Update<N>, ControllerError> {
        self.check_clock(now_ms)?;
        let Some(pending) = self.pending.take() else {
            return Ok(Update::None);
        };
        // A cancellation dispatched after the deadline is still a timeout.
        let failure = if now_ms >= pending.deadline {
            Failure::Device(Error::Timeout)
        } else {
            Failure::Cancelled
        };
        self.start_recovery(pending.id, now_ms);
        Ok(Update::Finished(Outcome {
            id: pending.id,
            result: Err(failure),
        }))
    }

    /// Process at most one event/outcome. Drain until None before waiting;
    /// Ignored is progress, not an empty queue (wakeups may be coalesced).
    pub fn poll(&mut self, now_ms: u64) -> Result<Update<N>, ControllerError> {
        self.check_clock(now_ms)?;
        if self.pending.as_ref().is_some_and(|p| now_ms >= p.deadline) {
            return self.cancel(now_ms);
        }
        if !self.recovery_failed && self.recovery.as_ref().is_some_and(|r| now_ms >= r.deadline) {
            self.recovery_failed = true;
            self.fault.get_or_insert(Failure::Device(Error::Timeout));
            return Ok(Update::RecoveryTimedOut);
        }
        let Some(event) = self.driver.poll() else {
            return Ok(Update::None);
        };
        if event.session != self.session {
            return Ok(Update::Ignored);
        }
        match event.kind {
            EventKind::Quiescent { recovery } => {
                if !self.recovery.as_ref().is_some_and(|r| r.id == recovery) {
                    return Ok(Update::Ignored);
                }
                self.recovery = None;
                Ok(if self.recovery_failed {
                    Update::Released
                } else {
                    Update::Recovered
                })
            }
            EventKind::Disconnected { connection, reason } => {
                let connecting = self
                    .pending
                    .as_ref()
                    .filter(|p| matches!(p.kind, Kind::Connect) && p.id == connection)
                    .map(|p| LinkId(p.id));
                let Some(link) = self.link.filter(|link| link.0 == connection).or(connecting)
                else {
                    return Ok(Update::Ignored);
                };
                if self
                    .pending
                    .as_ref()
                    .is_some_and(|p| matches!(p.kind, Kind::Disconnect))
                {
                    // Link loss is expected for a requested disconnect, but
                    // does not yet certify that callbacks have drained.
                    self.link = None;
                    return Ok(Update::Disconnected { link, reason });
                }
                let pending = self.pending.take();
                self.start_recovery(pending.as_ref().map_or(link.0, |p| p.id), now_ms);
                Ok(match pending {
                    Some(p) => Update::Finished(Outcome {
                        id: p.id,
                        result: Err(Failure::Disconnected),
                    }),
                    None => Update::Disconnected { link, reason },
                })
            }
            EventKind::Completed { request, result } => {
                if !self.pending.as_ref().is_some_and(|p| p.id == request) {
                    return Ok(Update::Ignored);
                }
                let pending = self.pending.take().unwrap();
                let result = match result {
                    Err(error) => Err(error),
                    Ok(reply) => self.complete(pending.kind, pending.id, reply),
                };
                // An ordinary remote ATT failure does not poison a connected
                // link. Local failures/cancellation require a drain and reset.
                let needs_recovery = match result {
                    Err(Failure::Att(_) | Failure::Remote(_)) => {
                        matches!(pending.kind, Kind::Connect | Kind::Disconnect)
                    }
                    Err(_) => true,
                    Ok(_) => false,
                };
                if needs_recovery {
                    if result == Err(Failure::Protocol) {
                        self.fault.get_or_insert(Failure::Protocol);
                    }
                    self.start_recovery(request, now_ms);
                }
                Ok(Update::Finished(Outcome {
                    id: pending.id,
                    result,
                }))
            }
        }
    }

    fn complete(
        &mut self,
        kind: Kind,
        id: RequestId,
        reply: Reply<N>,
    ) -> Result<Completion<N>, Failure> {
        Ok(match (kind, reply) {
            (Kind::Connect, Reply::Connected) => {
                let link = LinkId(id);
                self.link = Some(link);
                Completion::Connected(link)
            }
            (Kind::Discover, Reply::Discovered { handle }) => Completion::Discovered(Attribute {
                link: self.link.ok_or(Failure::Protocol)?,
                handle,
            }),
            (Kind::Read, Reply::Read(value)) => Completion::Read(value),
            (Kind::Write, Reply::Written) => Completion::Written,
            (Kind::Disconnect, Reply::Disconnected) => {
                self.link = None;
                Completion::Disconnected
            }
            _ => return Err(Failure::Protocol),
        })
    }

    fn start_recovery(&mut self, id: RequestId, now_ms: u64) {
        self.link = None; // Invalidate every old Attribute immediately.
        self.recovery = Some(Recovery {
            id,
            deadline: now_ms.saturating_add(self.recovery_timeout_ms),
        });
        self.driver.recover(id);
    }

    fn check_clock(&mut self, now_ms: u64) -> Result<(), ControllerError> {
        if now_ms < self.last_now_ms {
            return Err(ControllerError::ClockWentBackwards);
        }
        self.last_now_ms = now_ms;
        Ok(())
    }
}
