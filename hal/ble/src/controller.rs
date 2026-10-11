//! Shared BLE operation tracking, independent of provider and execution model.
//! The caller supplies events and monotonic time; no thread, queue or retry policy.
#![forbid(unsafe_code)]

use crate::{
    Ble, Capabilities, Command, Completion, Error, Event, EventKind, OperationId, ProviderStatus,
    Rejected, Request, RequestId, SessionId,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ControllerError {
    Device(Error),
    ClockWentBackwards,
    InvalidTimeout,
    IdExhausted,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    StartScan,
    StopScan,
    Connect(crate::PeerId),
    Disconnect(crate::PeerId),
    DiscoverServices(crate::PeerId),
    DiscoverCharacteristics(crate::PeerId, crate::ServiceId),
    Read(crate::PeerId, crate::CharacteristicId),
    Write(crate::PeerId, crate::CharacteristicId, crate::WriteMode),
    Subscribe(crate::PeerId, crate::CharacteristicId),
}

impl Kind {
    fn from_command<const N: usize>(command: &Command<N>) -> Self {
        match command {
            Command::StartScan { .. } => Self::StartScan,
            Command::StopScan => Self::StopScan,
            Command::Connect(peer) => Self::Connect(*peer),
            Command::Disconnect(peer) => Self::Disconnect(*peer),
            Command::DiscoverServices(peer) => Self::DiscoverServices(*peer),
            Command::DiscoverCharacteristics { peer, service } => {
                Self::DiscoverCharacteristics(*peer, *service)
            }
            Command::Read {
                peer,
                characteristic,
            } => Self::Read(*peer, *characteristic),
            Command::Write {
                peer,
                characteristic,
                mode,
                ..
            } => Self::Write(*peer, *characteristic, *mode),
            Command::Subscribe {
                peer,
                characteristic,
                ..
            } => Self::Subscribe(*peer, *characteristic),
        }
    }

    fn accepts<const N: usize>(self, event: EventKind<N>) -> bool {
        match (self, event) {
            (Self::DiscoverServices(peer), EventKind::Service { peer: actual, .. }) => {
                peer == actual
            }
            (
                Self::DiscoverCharacteristics(peer, service),
                EventKind::Characteristic {
                    peer: actual_peer,
                    service: actual_service,
                    ..
                },
            ) => peer == actual_peer && service == actual_service,
            _ => false,
        }
    }

    fn completion_matches<const N: usize>(self, completion: Completion<N>) -> bool {
        match (self, completion) {
            (Self::StartScan, Completion::ScanStarted)
            | (Self::StopScan, Completion::ScanStopped)
            | (Self::DiscoverServices(_), Completion::Discovery { .. })
            | (Self::DiscoverCharacteristics(_, _), Completion::Discovery { .. })
            | (Self::Read(_, _), Completion::Read(_))
            | (Self::Subscribe(_, _), Completion::SubscriptionChanged) => true,
            (Self::Connect(expected), Completion::Connected(actual))
            | (Self::Disconnect(expected), Completion::Disconnected(actual)) => expected == actual,
            (Self::Write(_, _, expected), Completion::Written(actual)) => expected == actual,
            _ => false,
        }
    }
}

struct Pending {
    id: RequestId,
    kind: Kind,
    deadline_ms: u64,
    outcome_reported: bool,
    mismatched_completion: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Outcome<const N: usize> {
    pub id: RequestId,
    pub result: Result<Completion<N>, Error>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Update<const N: usize> {
    Ignored,
    StatusChanged,
    /// A session-wide event, or a discovery item correlated to the active request.
    Event(EventKind<N>),
    Finished(Outcome<N>),
    /// Native completion used the right operation ID but the wrong result kind.
    MismatchedCompletion(RequestId),
    /// The request already timed out, but its native operation has now completed.
    Released(RequestId),
}

/// Owns a BLE provider and tracks one native operation at a time. Drive it from
/// the consuming application's or service's existing event loop.
pub struct Controller<D, const N: usize> {
    device: D,
    session: SessionId,
    status: ProviderStatus,
    fault: Option<Error>,
    next_operation: u64,
    pending: Option<Pending>,
    last_now_ms: u64,
    finished: bool,
}

impl<D: Ble<N>, const N: usize> Controller<D, N> {
    pub fn new(device: D) -> Self {
        let status = device.status();
        let session = device.session();
        let fault = device
            .fault()
            .or(if let ProviderStatus::Failed(error) = status {
                Some(error)
            } else {
                None
            });
        Self {
            device,
            session,
            status,
            fault,
            next_operation: 0,
            pending: None,
            last_now_ms: 0,
            finished: false,
        }
    }

    pub fn session(&self) -> SessionId {
        self.session
    }

    pub fn capabilities(&self) -> Capabilities {
        self.device.capabilities()
    }

    pub fn status(&self) -> ProviderStatus {
        self.status
    }

    /// First terminal provider failure remains visible through shutdown.
    pub fn fault(&self) -> Option<Error> {
        self.fault
    }

    pub fn is_busy(&self) -> bool {
        self.pending.is_some()
    }

    pub fn submit(
        &mut self,
        command: Command<N>,
        now_ms: u64,
        timeout_ms: u64,
    ) -> Result<RequestId, Rejected<Command<N>, ControllerError>> {
        let admission = self.prepare(&command, now_ms, timeout_ms);
        let (id, kind, deadline_ms, operation_value) = match admission {
            Ok(value) => value,
            Err(error) => {
                return Err(Rejected {
                    value: command,
                    error,
                });
            }
        };

        // IDs belong to this session forever, including IDs whose provider
        // admission is rejected. This makes delayed events unambiguous.
        self.next_operation = operation_value;
        match self.device.submit(Request { id, command }) {
            Ok(()) => {
                self.pending = Some(Pending {
                    id,
                    kind,
                    deadline_ms,
                    outcome_reported: false,
                    mismatched_completion: false,
                });
                Ok(id)
            }
            Err(rejected) => Err(Rejected {
                value: rejected.value.command,
                error: ControllerError::Device(rejected.error),
            }),
        }
    }

    fn prepare(
        &mut self,
        command: &Command<N>,
        now_ms: u64,
        timeout_ms: u64,
    ) -> Result<(RequestId, Kind, u64, u64), ControllerError> {
        self.check_clock(now_ms)?;
        if self.status != ProviderStatus::Ready {
            return Err(ControllerError::Device(Error::Unavailable));
        }
        if self.pending.is_some() {
            return Err(ControllerError::Device(Error::Busy));
        }
        let capabilities = self.device.capabilities();
        let supported = match command {
            Command::StartScan { .. } | Command::StopScan => capabilities.scan,
            _ => capabilities.central,
        };
        if !supported {
            return Err(ControllerError::Device(Error::Unsupported));
        }
        if timeout_ms == 0 {
            return Err(ControllerError::InvalidTimeout);
        }
        let deadline_ms = now_ms
            .checked_add(timeout_ms)
            .ok_or(ControllerError::InvalidTimeout)?;
        let operation = self
            .next_operation
            .checked_add(1)
            .and_then(OperationId::new)
            .ok_or(ControllerError::IdExhausted)?;
        let operation_value = self.next_operation + 1;
        Ok((
            RequestId {
                session: self.session,
                operation,
            },
            Kind::from_command(command),
            deadline_ms,
            operation_value,
        ))
    }

    /// Timeout reports to the caller but retains the native slot until its
    /// completion arrives or the provider reaches quiescent Stopped.
    pub fn next_deadline_ms(&self) -> Option<u64> {
        self.pending
            .as_ref()
            .filter(|pending| !pending.outcome_reported)
            .map(|pending| pending.deadline_ms)
    }

    pub fn advance(&mut self, now_ms: u64) -> Result<Update<N>, ControllerError> {
        self.check_clock(now_ms)?;
        if let Some(pending) = &mut self.pending {
            if !pending.outcome_reported && now_ms >= pending.deadline_ms {
                pending.outcome_reported = true;
                return Ok(Update::Finished(Outcome {
                    id: pending.id,
                    result: Err(Error::Timeout),
                }));
            }
        }
        Ok(Update::Ignored)
    }

    /// Recover sticky provider state after an event could not be admitted.
    pub fn reconcile(&mut self) -> Result<Update<N>, ControllerError> {
        if let Some(error) = self.device.fault() {
            self.fault.get_or_insert(error);
        }
        let status = self.device.status();
        Ok(self.observe_status(status))
    }

    /// Session mismatches and non-current operation events are ignored. Peer
    /// and service instance validation remains the provider's authority.
    pub fn on_event(&mut self, event: Event<N>, now_ms: u64) -> Result<Update<N>, ControllerError> {
        self.check_clock(now_ms)?;
        if event.session != self.session {
            return Ok(Update::Ignored);
        }
        match event.kind {
            EventKind::Status(status) => Ok(self.observe_status(status)),
            EventKind::Service { operation, .. } | EventKind::Characteristic { operation, .. } => {
                let Some(pending) = self.pending.as_mut() else {
                    return Ok(Update::Ignored);
                };
                if pending.id.operation != operation || pending.mismatched_completion {
                    return Ok(Update::Ignored);
                }
                if !pending.outcome_reported && now_ms >= pending.deadline_ms {
                    pending.outcome_reported = true;
                    return Ok(Update::Finished(Outcome {
                        id: pending.id,
                        result: Err(Error::Timeout),
                    }));
                }
                if !pending.outcome_reported && pending.kind.accepts(event.kind) {
                    Ok(Update::Event(event.kind))
                } else {
                    Ok(Update::Ignored)
                }
            }
            EventKind::Completed { operation, result } => {
                let Some(pending) = self.pending.as_ref() else {
                    return Ok(Update::Ignored);
                };
                if pending.id.operation != operation {
                    return Ok(Update::Ignored);
                }
                if pending.mismatched_completion {
                    return Ok(Update::Ignored);
                }
                let completion_matches = match result {
                    Err(_) => true,
                    Ok(completion) => pending.kind.completion_matches(completion),
                };
                if !completion_matches {
                    let id = pending.id;
                    let pending = self.pending.as_mut().unwrap();
                    pending.outcome_reported = true;
                    pending.mismatched_completion = true;
                    self.status = ProviderStatus::Failed(Error::Io);
                    self.fault.get_or_insert(Error::Io);
                    return Ok(Update::MismatchedCompletion(id));
                }
                let pending = self.pending.take().unwrap();
                if pending.outcome_reported {
                    return Ok(Update::Released(pending.id));
                }
                let result = if now_ms >= pending.deadline_ms {
                    Err(Error::Timeout)
                } else {
                    result
                };
                Ok(Update::Finished(Outcome {
                    id: pending.id,
                    result,
                }))
            }
            EventKind::Discovered { .. }
            | EventKind::Disconnected { .. }
            | EventKind::Value { .. } => {
                if matches!(
                    self.status,
                    ProviderStatus::Stopped | ProviderStatus::Failed(_)
                ) {
                    Ok(Update::Ignored)
                } else {
                    Ok(Update::Event(event.kind))
                }
            }
        }
    }

    fn observe_status(&mut self, status: ProviderStatus) -> Update<N> {
        if self.status == ProviderStatus::Stopped {
            return Update::Ignored;
        }
        if self.status == ProviderStatus::Ready && status == ProviderStatus::Starting {
            return Update::Ignored;
        }
        if matches!(status, ProviderStatus::Starting | ProviderStatus::Ready)
            && matches!(
                self.status,
                ProviderStatus::Stopping | ProviderStatus::Stopped | ProviderStatus::Failed(_)
            )
        {
            return Update::Ignored;
        }
        let changed = self.status != status;
        self.status = status;
        if let ProviderStatus::Failed(error) = status {
            self.fault.get_or_insert(error);
        }
        if matches!(status, ProviderStatus::Stopped | ProviderStatus::Failed(_)) {
            if let Some(pending) = self.pending.as_mut() {
                if !pending.outcome_reported {
                    pending.outcome_reported = true;
                    let outcome = Outcome {
                        id: pending.id,
                        result: Err(match status {
                            ProviderStatus::Failed(error) => error,
                            _ => Error::Unavailable,
                        }),
                    };
                    if status == ProviderStatus::Stopped {
                        self.pending = None;
                    }
                    return Update::Finished(outcome);
                }
            }
            if status == ProviderStatus::Stopped {
                if let Some(pending) = self.pending.take() {
                    return Update::Released(pending.id);
                }
            }
        }
        if changed {
            Update::StatusChanged
        } else {
            Update::Ignored
        }
    }

    /// Idempotent after acceptance. A failed request leaves ownership intact.
    pub fn request_stop(&mut self) -> Result<(), Error> {
        if matches!(
            self.status,
            ProviderStatus::Stopping | ProviderStatus::Stopped
        ) {
            return Ok(());
        }
        self.device.request_stop()?;
        self.status = ProviderStatus::Stopping;
        Ok(())
    }

    /// Teardown only, after Stopped confirms provider callbacks are quiescent.
    pub fn finish(&mut self) -> Result<(), Error> {
        if self.status != ProviderStatus::Stopped {
            return Err(Error::Busy);
        }
        if !self.finished {
            self.device.finish()?;
            self.finished = true;
        }
        Ok(())
    }

    fn check_clock(&mut self, now_ms: u64) -> Result<(), ControllerError> {
        if now_ms < self.last_now_ms {
            return Err(ControllerError::ClockWentBackwards);
        }
        self.last_now_ms = now_ms;
        Ok(())
    }
}
