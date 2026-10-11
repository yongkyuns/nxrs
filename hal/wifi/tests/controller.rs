//! Deterministic provider contract tests. This fake has no native radio support.
use std::cell::{Cell, RefCell};
use std::rc::Rc;

use wifi::*;

#[test]
fn controller_exposes_provider_identity_and_capabilities() {
    let fixture = Fixture::new(ProviderStatus::Ready);
    assert_eq!(fixture.controller.session(), fixture.session);
    assert_eq!(
        fixture.controller.capabilities(),
        Capabilities {
            scan: true,
            open_network: true,
            wpa2_personal: true,
        }
    );
}

struct Shared {
    status: Cell<ProviderStatus>,
    fault: Cell<Option<Error>>,
    reject: Cell<Option<Error>>,
    stop_error: Cell<Option<Error>>,
    stop_calls: Cell<usize>,
    finish_error: Cell<Option<Error>>,
    finish_calls: Cell<usize>,
    submitted: RefCell<Vec<Request>>,
}

struct FakeWifi {
    session: SessionId,
    capabilities: Capabilities,
    shared: Rc<Shared>,
}

impl Wifi for FakeWifi {
    fn session(&self) -> SessionId {
        self.session
    }
    fn capabilities(&self) -> Capabilities {
        self.capabilities
    }
    fn status(&self) -> ProviderStatus {
        self.shared.status.get()
    }
    fn fault(&self) -> Option<Error> {
        self.shared.fault.get()
    }
    fn submit(&mut self, request: Request) -> Result<(), Rejected<Command, Error>> {
        if let Some(error) = self.shared.reject.take() {
            return Err(Rejected {
                value: request.command,
                error,
            });
        }
        self.shared.submitted.borrow_mut().push(request);
        Ok(())
    }
    fn request_stop(&mut self) -> Result<(), Error> {
        self.shared.stop_calls.set(self.shared.stop_calls.get() + 1);
        if let Some(error) = self.shared.stop_error.take() {
            return Err(error);
        }
        self.shared.status.set(ProviderStatus::Stopping);
        Ok(())
    }
    fn finish(&mut self) -> Result<(), Error> {
        self.shared
            .finish_calls
            .set(self.shared.finish_calls.get() + 1);
        if let Some(error) = self.shared.finish_error.take() {
            return Err(error);
        }
        Ok(())
    }
}

struct Fixture {
    controller: Controller<FakeWifi>,
    shared: Rc<Shared>,
    session: SessionId,
}

impl Fixture {
    fn new(status: ProviderStatus) -> Self {
        Self::with_session(status, SessionId::new(1).unwrap())
    }

    fn with_session(status: ProviderStatus, session: SessionId) -> Self {
        let shared = Rc::new(Shared {
            status: Cell::new(status),
            fault: Cell::new(None),
            reject: Cell::new(None),
            stop_error: Cell::new(None),
            stop_calls: Cell::new(0),
            finish_error: Cell::new(None),
            finish_calls: Cell::new(0),
            submitted: RefCell::new(Vec::new()),
        });
        let controller = Controller::new(FakeWifi {
            session,
            shared: shared.clone(),
            capabilities: Capabilities {
                scan: true,
                open_network: true,
                wpa2_personal: true,
            },
        });
        Self {
            controller,
            shared,
            session,
        }
    }

    fn event(&mut self, kind: EventKind, now_ms: u64) -> Result<Update, ControllerError> {
        self.controller.on_event(
            Event {
                session: self.session,
                kind,
            },
            now_ms,
        )
    }

    fn status(&mut self, status: ProviderStatus, now_ms: u64) -> Update {
        self.shared.status.set(status);
        self.event(EventKind::Status(status), now_ms).unwrap()
    }

    fn complete(
        &mut self,
        id: RequestId,
        result: Result<Completion, Error>,
        now_ms: u64,
    ) -> Result<Update, ControllerError> {
        self.event(
            EventKind::Completed {
                operation: id.operation,
                result,
            },
            now_ms,
        )
    }
}

fn connect() -> Command {
    Command::Connect {
        ssid: Ssid::new(b"fixture-network").unwrap(),
        credentials: Credentials::Wpa2Personal(Passphrase::new("fixture-password").unwrap()),
    }
}

fn assert_connect_returned(command: Command) {
    let Command::Connect {
        ssid,
        credentials: Credentials::Wpa2Personal(passphrase),
    } = command
    else {
        panic!("lost original connection request")
    };
    assert_eq!(ssid.as_bytes(), b"fixture-network");
    assert_eq!(passphrase.as_bytes(), b"fixture-password");
}

#[test]
fn startup_and_link_readiness_are_separate() {
    let mut f = Fixture::new(ProviderStatus::Starting);
    assert_eq!(f.controller.link(), LinkState::Unknown);
    let rejected = f.controller.submit(connect(), 0, 100).unwrap_err();
    assert_eq!(rejected.error, ControllerError::Device(Error::Unavailable));
    assert_connect_returned(rejected.value);
    assert_eq!(f.status(ProviderStatus::Ready, 0), Update::StatusChanged);
    let id = f.controller.submit(connect(), 1, 100).unwrap();
    assert_eq!(
        f.complete(id, Ok(Completion::Connected), 2).unwrap(),
        Update::Finished(Outcome {
            id,
            result: Ok(Completion::Connected)
        })
    );
    // Operation success does not fabricate an authoritative IP/link snapshot.
    assert_eq!(f.controller.link(), LinkState::Unknown);
    f.event(EventKind::LinkChanged(LinkState::Associated), 3)
        .unwrap();
    assert_eq!(f.controller.link(), LinkState::Associated);
    f.event(EventKind::LinkChanged(LinkState::Addressed), 4)
        .unwrap();
    assert_eq!(f.controller.link(), LinkState::Addressed);
}

#[test]
fn scan_results_and_truncation_are_explicit() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(Command::Scan, 0, 100).unwrap();
    let ap = AccessPoint {
        ssid: None,
        security: Security::Other,
        rssi_dbm: None,
    };
    assert_eq!(
        f.event(
            EventKind::ScanResult {
                operation: id.operation,
                access_point: ap
            },
            1
        )
        .unwrap(),
        Update::ScanResult(ap)
    );
    let completion = Completion::Scan {
        reported: 1,
        truncated: true,
    };
    assert_eq!(
        f.complete(id, Ok(completion), 2).unwrap(),
        Update::Finished(Outcome {
            id,
            result: Ok(completion)
        })
    );
    assert!(!f.controller.is_busy());
    assert_eq!(f.controller.next_deadline_ms(), None);
}

#[test]
fn timeout_is_reported_once_without_freeing_native_capacity() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(Command::Scan, 10, 20).unwrap();
    assert_eq!(f.controller.next_deadline_ms(), Some(30));
    assert_eq!(f.controller.advance(29).unwrap(), Update::Ignored);
    assert_eq!(
        f.controller.advance(30).unwrap(),
        Update::Finished(Outcome {
            id,
            result: Err(Error::Timeout)
        })
    );
    assert_eq!(f.controller.next_deadline_ms(), None);
    assert_eq!(f.controller.advance(31).unwrap(), Update::Ignored);
    let rejected = f.controller.submit(connect(), 31, 20).unwrap_err();
    assert_eq!(rejected.error, ControllerError::Device(Error::Busy));
    assert_connect_returned(rejected.value);
    assert_eq!(
        f.complete(
            id,
            Ok(Completion::Scan {
                reported: 0,
                truncated: false
            }),
            32
        )
        .unwrap(),
        Update::Released(id)
    );
    assert!(!f.controller.is_busy());
}

#[test]
fn old_completions_cannot_complete_a_new_request() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let old = f.controller.submit(Command::Scan, 0, 10).unwrap();
    f.controller.advance(10).unwrap();
    f.complete(
        old,
        Ok(Completion::Scan {
            reported: 0,
            truncated: false,
        }),
        11,
    )
    .unwrap();
    let new = f.controller.submit(connect(), 11, 100).unwrap();
    assert_ne!(old, new);
    assert_eq!(
        f.complete(old, Err(Error::Io), 12).unwrap(),
        Update::Ignored
    );
    assert!(f.controller.is_busy());
    assert_eq!(
        f.complete(new, Ok(Completion::Connected), 13).unwrap(),
        Update::Finished(Outcome {
            id: new,
            result: Ok(Completion::Connected)
        })
    );
}

#[test]
fn completion_at_deadline_is_timeout_even_without_prior_timer_dispatch() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(connect(), 0, 10).unwrap();
    assert_eq!(
        f.complete(id, Ok(Completion::Connected), 10).unwrap(),
        Update::Finished(Outcome {
            id,
            result: Err(Error::Timeout)
        })
    );
    assert!(!f.controller.is_busy());
    assert_eq!(f.controller.advance(10).unwrap(), Update::Ignored);
    // Native side effects remain observable despite the logical timeout.
    f.event(EventKind::LinkChanged(LinkState::Associated), 11)
        .unwrap();
    assert_eq!(f.controller.link(), LinkState::Associated);
}

#[test]
fn stale_session_events_cannot_stop_or_connect_the_current_session() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(connect(), 0, 100).unwrap();
    for kind in [
        EventKind::Status(ProviderStatus::Stopped),
        EventKind::LinkChanged(LinkState::Addressed),
        EventKind::Completed {
            operation: id.operation,
            result: Ok(Completion::Connected),
        },
    ] {
        assert_eq!(
            f.controller
                .on_event(
                    Event {
                        session: SessionId::new(2).unwrap(),
                        kind
                    },
                    1
                )
                .unwrap(),
            Update::Ignored
        );
    }
    assert_eq!(f.controller.status(), ProviderStatus::Ready);
    assert_eq!(f.controller.link(), LinkState::Unknown);
    assert!(f.controller.is_busy());
}

#[test]
fn native_rejection_returns_credentials_and_creates_no_pending_operation() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    f.shared.reject.set(Some(Error::PermissionDenied));
    let rejected = f.controller.submit(connect(), 0, 100).unwrap_err();
    assert_eq!(
        rejected.error,
        ControllerError::Device(Error::PermissionDenied)
    );
    assert_connect_returned(rejected.value);
    assert!(!f.controller.is_busy());
    assert!(f.shared.submitted.borrow().is_empty());
}

#[test]
fn authentication_failure_is_not_connection_success() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(connect(), 0, 100).unwrap();
    assert_eq!(
        f.complete(id, Err(Error::AuthenticationFailed), 1).unwrap(),
        Update::Finished(Outcome {
            id,
            result: Err(Error::AuthenticationFailed)
        })
    );
    assert_eq!(f.controller.link(), LinkState::Unknown);
    assert!(!f.controller.is_busy());
}

#[test]
fn wrong_completion_kind_faults_the_session_without_releasing_native_capacity() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(Command::Scan, 0, 100).unwrap();
    assert_eq!(
        f.complete(id, Ok(Completion::Connected), 1),
        Ok(Update::Finished(Outcome {
            id,
            result: Err(Error::Protocol)
        }))
    );
    assert!(f.controller.is_busy());
    assert_eq!(f.controller.next_deadline_ms(), None);
    assert_eq!(f.controller.fault(), Some(Error::Protocol));
    assert!(f.controller.submit(Command::Scan, 2, 100).is_err());
    f.controller.request_stop().unwrap();
    assert_eq!(f.status(ProviderStatus::Stopped, 3), Update::StatusChanged);
    assert!(!f.controller.is_busy());
}

#[test]
fn shutdown_closes_admission_until_quiescent_stop() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(connect(), 0, 100).unwrap();
    f.controller.request_stop().unwrap();
    f.controller.request_stop().unwrap();
    assert_eq!(f.shared.stop_calls.get(), 1);
    assert_eq!(f.controller.status(), ProviderStatus::Stopping);
    assert!(f.controller.is_busy());
    let rejected = f.controller.submit(Command::Scan, 1, 100).unwrap_err();
    assert_eq!(rejected.error, ControllerError::Device(Error::Unavailable));
    assert_eq!(
        f.status(ProviderStatus::Stopped, 2),
        Update::Finished(Outcome {
            id,
            result: Err(Error::Unavailable)
        })
    );
    assert!(!f.controller.is_busy());
    for status in [
        ProviderStatus::Ready,
        ProviderStatus::Starting,
        ProviderStatus::Stopping,
        ProviderStatus::Failed(Error::Io),
    ] {
        assert_eq!(
            f.event(EventKind::Status(status), 3).unwrap(),
            Update::Ignored
        );
        assert_eq!(f.controller.status(), ProviderStatus::Stopped);
    }
    assert_eq!(
        f.complete(id, Ok(Completion::Connected), 3).unwrap(),
        Update::Ignored
    );
}

#[test]
fn shutdown_failure_is_retryable_and_does_not_fake_acceptance() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    f.shared.stop_error.set(Some(Error::Io));
    assert_eq!(f.controller.request_stop(), Err(Error::Io));
    assert_eq!(f.controller.status(), ProviderStatus::Ready);
    f.controller.request_stop().unwrap();
    assert_eq!(f.shared.stop_calls.get(), 2);
    assert_eq!(f.controller.status(), ProviderStatus::Stopping);
}

#[test]
fn a_full_event_queue_does_not_hide_sticky_failure_or_stop() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(connect(), 0, 100).unwrap();
    let (sender, _receiver) = std::sync::mpsc::sync_channel(1);
    sender
        .try_send(EventKind::Status(ProviderStatus::Ready))
        .unwrap();
    f.shared.status.set(ProviderStatus::Failed(Error::Io));
    assert!(matches!(
        sender.try_send(EventKind::Status(ProviderStatus::Failed(Error::Io))),
        Err(std::sync::mpsc::TrySendError::Full(_))
    ));
    // The queued event wakes the owner, which checks sticky state after dispatch.
    assert_eq!(
        f.controller.reconcile().unwrap(),
        Update::Finished(Outcome {
            id,
            result: Err(Error::Io)
        })
    );
    assert_eq!(f.controller.reconcile().unwrap(), Update::Ignored);
    assert_eq!(f.controller.fault(), Some(Error::Io));
    assert!(f.controller.is_busy()); // Failure is not quiescence.
    f.controller.request_stop().unwrap();
    f.shared.status.set(ProviderStatus::Stopped);
    assert_eq!(f.controller.reconcile().unwrap(), Update::StatusChanged);
    assert!(!f.controller.is_busy());
    assert_eq!(f.controller.fault(), Some(Error::Io));
}

#[test]
fn delayed_starting_cannot_regress_a_ready_session() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    assert_eq!(
        f.event(EventKind::Status(ProviderStatus::Starting), 0)
            .unwrap(),
        Update::Ignored
    );
    assert_eq!(f.controller.status(), ProviderStatus::Ready);
}

#[test]
fn a_lost_fault_is_recovered_after_quiescence_and_reclamation() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(Command::Scan, 0, 100).unwrap();
    // Failure and Stopped occurred before the owner processed any wakeup.
    f.shared.fault.set(Some(Error::Io));
    f.shared.status.set(ProviderStatus::Stopped);
    assert_eq!(
        f.controller.reconcile().unwrap(),
        Update::Finished(Outcome {
            id,
            result: Err(Error::Io),
        })
    );
    assert_eq!(f.controller.fault(), Some(Error::Io));
    assert!(!f.controller.is_busy());
    f.controller.finish().unwrap();
    assert_eq!(f.controller.reconcile().unwrap(), Update::Ignored);
    assert_eq!(f.controller.fault(), Some(Error::Io));
}

#[test]
fn unsupported_capabilities_do_not_submit_native_work() {
    let shared = Fixture::new(ProviderStatus::Ready).shared;
    let mut controller = Controller::new(FakeWifi {
        session: SessionId::new(1).unwrap(),
        shared: shared.clone(),
        capabilities: Capabilities {
            scan: false,
            open_network: false,
            wpa2_personal: false,
        },
    });
    assert_eq!(
        controller.submit(Command::Scan, 0, 100).unwrap_err().error,
        ControllerError::Device(Error::Unsupported)
    );
    let rejected = controller.submit(connect(), 0, 100).unwrap_err();
    assert_eq!(rejected.error, ControllerError::Device(Error::Unsupported));
    assert_connect_returned(rejected.value);
    assert!(shared.submitted.borrow().is_empty());
}

#[test]
fn request_ids_are_echoed_from_admission_through_provider_and_outcome() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(Command::Disconnect, 0, 100).unwrap();
    assert_eq!(f.shared.submitted.borrow()[0].id, id);
    assert_eq!(
        f.complete(id, Ok(Completion::Disconnected), 1).unwrap(),
        Update::Finished(Outcome {
            id,
            result: Ok(Completion::Disconnected)
        })
    );
}

#[test]
fn restart_uses_a_new_session_even_when_operation_numbers_match() {
    let mut old = Fixture::new(ProviderStatus::Ready);
    let old_id = old.controller.submit(connect(), 0, 100).unwrap();
    old.controller.request_stop().unwrap();
    old.status(ProviderStatus::Stopped, 1);
    old.controller.finish().unwrap();

    let mut new = Fixture::with_session(ProviderStatus::Ready, SessionId::new(2).unwrap());
    let new_id = new.controller.submit(connect(), 0, 100).unwrap();
    assert_eq!(old_id.operation, new_id.operation);
    assert_ne!(old_id, new_id);
    assert_eq!(
        new.controller
            .on_event(
                Event {
                    session: old_id.session,
                    kind: EventKind::Completed {
                        operation: old_id.operation,
                        result: Ok(Completion::Connected)
                    },
                },
                1
            )
            .unwrap(),
        Update::Ignored
    );
    assert!(new.controller.is_busy());
}

#[test]
fn resource_reclamation_requires_quiescence_and_is_idempotent() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    assert_eq!(f.controller.finish(), Err(Error::Busy));
    f.controller.request_stop().unwrap();
    assert_eq!(f.controller.finish(), Err(Error::Busy));
    assert_eq!(f.shared.finish_calls.get(), 0);
    f.status(ProviderStatus::Stopped, 1);
    f.controller.finish().unwrap();
    f.controller.finish().unwrap();
    assert_eq!(f.shared.finish_calls.get(), 1);
}

#[test]
fn reclamation_failure_retains_resources_for_retry() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    f.controller.request_stop().unwrap();
    f.status(ProviderStatus::Stopped, 1);
    f.shared.finish_error.set(Some(Error::Io));
    assert_eq!(f.controller.finish(), Err(Error::Io));
    f.controller.finish().unwrap();
    assert_eq!(f.shared.finish_calls.get(), 2);
}

#[test]
fn stopping_and_failure_cannot_be_resurrected_by_delayed_ready() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    f.controller.request_stop().unwrap();
    assert_eq!(
        f.event(EventKind::Status(ProviderStatus::Ready), 0)
            .unwrap(),
        Update::Ignored
    );
    assert_eq!(f.controller.status(), ProviderStatus::Stopping);
    f.status(ProviderStatus::Failed(Error::Io), 1);
    assert_eq!(
        f.event(EventKind::Status(ProviderStatus::Starting), 2)
            .unwrap(),
        Update::Ignored
    );
    assert_eq!(f.controller.status(), ProviderStatus::Failed(Error::Io));
}

#[test]
fn timed_out_requests_are_not_completed_again_on_stop() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    f.controller.submit(Command::Scan, 0, 10).unwrap();
    f.controller.advance(10).unwrap();
    f.controller.request_stop().unwrap();
    assert_eq!(f.status(ProviderStatus::Stopped, 11), Update::StatusChanged);
    assert!(!f.controller.is_busy());
}

#[test]
fn deadlines_reject_overflow_and_backwards_clocks() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    assert_eq!(
        f.controller.submit(Command::Scan, 5, 0).unwrap_err().error,
        ControllerError::InvalidTimeout
    );
    assert_eq!(
        f.controller.submit(Command::Scan, 4, 1).unwrap_err().error,
        ControllerError::ClockWentBackwards
    );
    assert!(!f.controller.is_busy());
    assert_eq!(
        f.controller
            .submit(Command::Scan, u64::MAX, 1)
            .unwrap_err()
            .error,
        ControllerError::InvalidTimeout
    );
}

#[test]
fn scan_results_are_ignored_after_timeout_or_for_another_operation() {
    let mut f = Fixture::new(ProviderStatus::Ready);
    let id = f.controller.submit(Command::Scan, 0, 10).unwrap();
    let access_point = AccessPoint {
        ssid: None,
        security: Security::Open,
        rssi_dbm: Some(-60),
    };
    for (operation, time) in [(OperationId::new(2).unwrap(), 1), (id.operation, 10)] {
        assert_eq!(
            f.event(
                EventKind::ScanResult {
                    operation,
                    access_point
                },
                time
            )
            .unwrap(),
            Update::Ignored
        );
    }
    assert!(matches!(
        f.controller.advance(10).unwrap(),
        Update::Finished(_)
    ));
}
