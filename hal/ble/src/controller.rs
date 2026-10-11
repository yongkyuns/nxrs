//! Shared BLE scan request tracking, independent of provider and execution model.
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
}

impl Kind {
    fn from_command(command: Command) -> Self {
        match command {
            Command::StartScan { .. } => Self::StartScan,
            Command::StopScan => Self::StopScan,
        }
    }
    fn completion_matches(self, completion: Completion) -> bool {
        matches!(
            (self, completion),
            (Self::StartScan, Completion::ScanStarted) | (Self::StopScan, Completion::ScanStopped)
        )
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
pub struct Outcome {
    pub id: RequestId,
    pub result: Result<Completion, Error>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Update {
    Ignored,
    StatusChanged,
    Event(EventKind),
    Finished(Outcome),
    MismatchedCompletion(RequestId),
    Released(RequestId),
}

/// Owns a BLE provider and tracks one native operation at a time.
pub struct Controller<D> {
    device: D,
    session: SessionId,
    status: ProviderStatus,
    fault: Option<Error>,
    next_operation: u64,
    pending: Option<Pending>,
    last_now_ms: u64,
    finished: bool,
}

impl<D: Ble> Controller<D> {
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
    pub fn fault(&self) -> Option<Error> {
        self.fault
    }
    pub fn is_busy(&self) -> bool {
        self.pending.is_some()
    }

    pub fn submit(
        &mut self,
        command: Command,
        now_ms: u64,
        timeout_ms: u64,
    ) -> Result<RequestId, Rejected<Command, ControllerError>> {
        let (id, kind, deadline_ms, next) = match self.prepare(command, now_ms, timeout_ms) {
            Ok(value) => value,
            Err(error) => {
                return Err(Rejected {
                    value: command,
                    error,
                })
            }
        };
        self.next_operation = next;
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
        command: Command,
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
        if !self.device.capabilities().scan {
            return Err(ControllerError::Device(Error::Unsupported));
        }
        if timeout_ms == 0 {
            return Err(ControllerError::InvalidTimeout);
        }
        let deadline = now_ms
            .checked_add(timeout_ms)
            .ok_or(ControllerError::InvalidTimeout)?;
        let next = self
            .next_operation
            .checked_add(1)
            .ok_or(ControllerError::IdExhausted)?;
        let operation = OperationId::new(next).ok_or(ControllerError::IdExhausted)?;
        Ok((
            RequestId {
                session: self.session,
                operation,
            },
            Kind::from_command(command),
            deadline,
            next,
        ))
    }

    pub fn next_deadline_ms(&self) -> Option<u64> {
        self.pending
            .as_ref()
            .filter(|p| !p.outcome_reported)
            .map(|p| p.deadline_ms)
    }
    pub fn advance(&mut self, now_ms: u64) -> Result<Update, ControllerError> {
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
    pub fn reconcile(&mut self) -> Result<Update, ControllerError> {
        Ok(self.observe_status(self.device.status()))
    }
    pub fn on_event(&mut self, event: Event, now_ms: u64) -> Result<Update, ControllerError> {
        self.check_clock(now_ms)?;
        if event.session != self.session {
            return Ok(Update::Ignored);
        }
        match event.kind {
            EventKind::Status(status) => Ok(self.observe_status(status)),
            EventKind::Discovered { .. } | EventKind::DiscoveryLimited { .. } => {
                if matches!(
                    self.status,
                    ProviderStatus::Stopped | ProviderStatus::Failed(_)
                ) {
                    Ok(Update::Ignored)
                } else {
                    Ok(Update::Event(event.kind))
                }
            }
            EventKind::Completed { operation, result } => {
                let Some(pending) = self.pending.as_ref() else {
                    return Ok(Update::Ignored);
                };
                if pending.id.operation != operation || pending.mismatched_completion {
                    return Ok(Update::Ignored);
                }
                if result.is_ok_and(|value| !pending.kind.completion_matches(value)) {
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
        }
    }
    fn observe_status(&mut self, status: ProviderStatus) -> Update {
        if let Some(error) = self.device.fault() {
            self.fault.get_or_insert(error);
        }
        if self.status == ProviderStatus::Stopped
            || (self.status == ProviderStatus::Ready && status == ProviderStatus::Starting)
        {
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
            let pending_outcome = self.pending.as_mut().and_then(|pending| {
                if pending.outcome_reported {
                    None
                } else {
                    pending.outcome_reported = true;
                    Some(Outcome {
                        id: pending.id,
                        result: Err(match status {
                            ProviderStatus::Failed(error) => error,
                            _ => self.fault.unwrap_or(Error::Unavailable),
                        }),
                    })
                }
            });
            if let Some(outcome) = pending_outcome {
                if status == ProviderStatus::Stopped {
                    self.pending = None;
                }
                return Update::Finished(outcome);
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
