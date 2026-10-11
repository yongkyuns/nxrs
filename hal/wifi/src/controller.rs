//! Shared Wi-Fi operation tracking, independent of provider and execution model.
//! The caller supplies events and monotonic time; no thread, queue or retry policy.
#![forbid(unsafe_code)]

use crate::{
    AccessPoint, Capabilities, Command, Completion, Credentials, Error, Event, EventKind,
    LinkState, OperationId, ProviderStatus, Rejected, Request, RequestId, SessionId, Wifi,
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
    Scan,
    Connect,
    Disconnect,
}

impl Kind {
    fn matches(self, result: Completion) -> bool {
        matches!(
            (self, result),
            (Self::Scan, Completion::Scan { .. })
                | (Self::Connect, Completion::Connected)
                | (Self::Disconnect, Completion::Disconnected)
        )
    }
}

struct Pending {
    id: RequestId,
    kind: Kind,
    deadline_ms: u64,
    outcome_reported: bool,
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
    ScanResult(AccessPoint),
    Finished(Outcome),
    /// An operation whose outcome was already reported has finished natively.
    Released(RequestId),
}

/// Owns a provider and normalizes operation tracking, deadlines and lifecycle.
/// Drive it from the consuming application's or service's existing event loop.
pub struct Controller<D> {
    device: D,
    session: SessionId,
    status: ProviderStatus,
    link: LinkState,
    fault: Option<Error>,
    next_operation: u64,
    pending: Option<Pending>,
    last_now_ms: u64,
    finished: bool,
}

impl<D: Wifi> Controller<D> {
    /// The owner must install provider delivery before enabling acquisition.
    pub fn new(device: D) -> Self {
        let status = device.status();
        let session = device.session();
        let fault = if let ProviderStatus::Failed(error) = status {
            Some(error)
        } else {
            None
        };
        Self {
            device,
            session,
            status,
            link: LinkState::Unknown,
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
    pub fn link(&self) -> LinkState {
        self.link
    }
    /// First terminal provider failure remains visible through shutdown.
    pub fn fault(&self) -> Option<Error> {
        self.fault
    }
    pub fn is_busy(&self) -> bool {
        self.pending.is_some()
    }

    /// None after a timeout avoids repeatedly selecting an expired deadline.
    pub fn next_deadline_ms(&self) -> Option<u64> {
        self.pending
            .as_ref()
            .filter(|pending| !pending.outcome_reported)
            .map(|pending| pending.deadline_ms)
    }

    /// One native operation at a time in this initial station contract. Rejected
    /// credentials are returned intact. Retry/reconnect decisions stay explicit.
    pub fn submit(
        &mut self,
        command: Command,
        now_ms: u64,
        timeout_ms: u64,
    ) -> Result<RequestId, Rejected<Command, ControllerError>> {
        let admission = self.prepare(&command, now_ms, timeout_ms);
        let (id, kind, deadline_ms) = match admission {
            Ok(value) => value,
            Err(error) => {
                return Err(Rejected {
                    value: command,
                    error,
                });
            }
        };
        self.next_operation += 1;
        match self.device.submit(Request { id, command }) {
            Ok(()) => {
                self.pending = Some(Pending {
                    id,
                    kind,
                    deadline_ms,
                    outcome_reported: false,
                });
                Ok(id)
            }
            Err(rejected) => Err(Rejected {
                value: rejected.value,
                error: ControllerError::Device(rejected.error),
            }),
        }
    }

    fn prepare(
        &mut self,
        command: &Command,
        now_ms: u64,
        timeout_ms: u64,
    ) -> Result<(RequestId, Kind, u64), ControllerError> {
        self.check_clock(now_ms)?;
        if self.status != ProviderStatus::Ready {
            return Err(ControllerError::Device(Error::Unavailable));
        }
        if self.pending.is_some() {
            return Err(ControllerError::Device(Error::Busy));
        }
        let capabilities = self.device.capabilities();
        let (kind, supported) = match command {
            Command::Scan => (Kind::Scan, capabilities.scan),
            Command::Connect {
                credentials: Credentials::Open,
                ..
            } => (Kind::Connect, capabilities.open_network),
            Command::Connect {
                credentials: Credentials::Wpa2Personal(_),
                ..
            } => (Kind::Connect, capabilities.wpa2_personal),
            Command::Disconnect => (Kind::Disconnect, true),
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
        Ok((
            RequestId {
                session: self.session,
                operation,
            },
            kind,
            deadline_ms,
        ))
    }

    /// Timeout completes the caller's request, not the native operation. Keep
    /// its slot until native completion or quiescent stop: no overlapping retry.
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

    pub fn on_event(&mut self, event: Event, now_ms: u64) -> Result<Update, ControllerError> {
        self.check_clock(now_ms)?;
        if event.session != self.session {
            return Ok(Update::Ignored);
        }
        match event.kind {
            EventKind::Status(status) => self.observe_status(status),
            EventKind::LinkChanged(link) => {
                if matches!(
                    self.status,
                    ProviderStatus::Stopped | ProviderStatus::Failed(_)
                ) {
                    return Ok(Update::Ignored);
                }
                self.link = link;
                Ok(Update::StatusChanged)
            }
            EventKind::ScanResult {
                operation,
                access_point,
            } => {
                if self.pending.as_ref().is_some_and(|pending| {
                    pending.id.operation == operation
                        && pending.kind == Kind::Scan
                        && !pending.outcome_reported
                        && now_ms < pending.deadline_ms
                }) {
                    Ok(Update::ScanResult(access_point))
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
                if result.is_ok_and(|value| !pending.kind.matches(value)) {
                    // Native state is no longer trustworthy. Report once and
                    // require explicit quiescent stop before reclaiming it.
                    return self.observe_status(ProviderStatus::Failed(Error::Protocol));
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

    /// Called after a provider wakeup if event admission failed. This reads a
    /// nonblocking sticky snapshot, not a periodic device-polling mechanism.
    pub fn reconcile(&mut self) -> Result<Update, ControllerError> {
        self.observe_status(self.device.status())
    }

    fn observe_status(&mut self, status: ProviderStatus) -> Result<Update, ControllerError> {
        // Quiescence is final for this session, including delayed fault events.
        if self.status == ProviderStatus::Stopped {
            return Ok(Update::Ignored);
        }
        if self.status == ProviderStatus::Ready && status == ProviderStatus::Starting {
            return Ok(Update::Ignored);
        }
        // Ready/Starting cannot resurrect a stopping, failed or stopped session.
        if matches!(status, ProviderStatus::Starting | ProviderStatus::Ready)
            && matches!(
                self.status,
                ProviderStatus::Stopping | ProviderStatus::Stopped | ProviderStatus::Failed(_)
            )
        {
            return Ok(Update::Ignored);
        }
        let changed = self.status != status;
        self.status = status;
        if let ProviderStatus::Failed(error) = status {
            self.fault.get_or_insert(error);
        }
        if matches!(status, ProviderStatus::Stopped | ProviderStatus::Failed(_)) {
            self.link = LinkState::Unknown;
            if let Some(pending) = self.pending.as_mut() {
                if !pending.outcome_reported {
                    pending.outcome_reported = true;
                    let error = match status {
                        ProviderStatus::Failed(error) => error,
                        _ => Error::Unavailable,
                    };
                    let outcome = Outcome {
                        id: pending.id,
                        result: Err(error),
                    };
                    if status == ProviderStatus::Stopped {
                        self.pending = None;
                    }
                    return Ok(Update::Finished(outcome));
                }
            }
            if status == ProviderStatus::Stopped {
                self.pending = None;
            }
        }
        Ok(if changed {
            Update::StatusChanged
        } else {
            Update::Ignored
        })
    }

    /// Idempotent after acceptance. Failure leaves ownership intact for retry.
    /// Worker joining belongs to the composition owner after quiescence.
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

    /// Teardown only, after leaving normal dispatch. No hidden join in Drop.
    /// Failure retains the provider for retry; success is idempotent.
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

#[cfg(test)]
mod tests {
    use super::*;

    struct Ready;
    impl Wifi for Ready {
        fn session(&self) -> SessionId {
            SessionId::new(1).unwrap()
        }
        fn capabilities(&self) -> crate::Capabilities {
            crate::Capabilities {
                scan: true,
                open_network: true,
                wpa2_personal: true,
            }
        }
        fn status(&self) -> ProviderStatus {
            ProviderStatus::Ready
        }
        fn submit(&mut self, _: Request) -> Result<(), Rejected<Command, Error>> {
            Ok(())
        }
        fn request_stop(&mut self) -> Result<(), Error> {
            Ok(())
        }
        fn finish(&mut self) -> Result<(), Error> {
            Ok(())
        }
    }

    #[test]
    fn operation_ids_never_wrap() {
        let mut controller = Controller::new(Ready);
        controller.next_operation = u64::MAX;
        assert_eq!(
            controller.submit(Command::Scan, 0, 1).unwrap_err().error,
            ControllerError::IdExhausted
        );
        assert!(!controller.is_busy());
    }
}
