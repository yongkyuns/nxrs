use ble::{
    Ble, Capabilities, Command, Completion, Controller, ControllerError, Error, Event, EventKind,
    ProviderStatus, Rejected, Request, RequestId, SessionId, Update,
};
use core::cell::Cell;
use std::rc::Rc;

struct MockBle {
    status: Rc<Cell<ProviderStatus>>,
    fault: Rc<Cell<Option<Error>>>,
    scan: bool,
    reject_next: bool,
    last_attempt: Rc<Cell<Option<RequestId>>>,
    stop_calls: usize,
    finish_calls: usize,
}
impl MockBle {
    fn ready() -> Self {
        Self {
            status: Rc::new(Cell::new(ProviderStatus::Ready)),
            fault: Rc::new(Cell::new(None)),
            scan: true,
            reject_next: false,
            last_attempt: Rc::new(Cell::new(None)),
            stop_calls: 0,
            finish_calls: 0,
        }
    }
}
impl Ble for MockBle {
    fn session(&self) -> SessionId {
        SessionId::new(7).unwrap()
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities { scan: self.scan }
    }
    fn status(&self) -> ProviderStatus {
        self.status.get()
    }
    fn fault(&self) -> Option<Error> {
        self.fault.get()
    }
    fn submit(&mut self, request: Request) -> Result<(), Rejected<Request, Error>> {
        self.last_attempt.set(Some(request.id));
        if self.reject_next {
            self.reject_next = false;
            Err(Rejected {
                value: request,
                error: Error::Busy,
            })
        } else {
            Ok(())
        }
    }
    fn request_stop(&mut self) -> Result<(), Error> {
        self.stop_calls += 1;
        self.status.set(ProviderStatus::Stopping);
        Ok(())
    }
    fn finish(&mut self) -> Result<(), Error> {
        self.finish_calls += 1;
        Ok(())
    }
}
fn completed(session: u64, request: RequestId) -> Event {
    Event {
        session: SessionId::new(session).unwrap(),
        kind: EventKind::Completed {
            operation: request.operation,
            result: Ok(Completion::ScanStarted),
        },
    }
}
fn status(status: ProviderStatus) -> Event {
    Event {
        session: SessionId::new(7).unwrap(),
        kind: EventKind::Status(status),
    }
}

#[test]
fn session_and_operation_mismatches_do_not_finish_current_scan() {
    let mut controller = Controller::new(MockBle::ready());
    let first = controller
        .submit(Command::StartScan { service: None }, 0, 20)
        .unwrap();
    assert_eq!(
        controller.on_event(completed(99, first), 1).unwrap(),
        Update::Ignored
    );
    assert!(matches!(
        controller.on_event(completed(7, first), 1).unwrap(),
        Update::Finished(_)
    ));
    let second = controller
        .submit(Command::StartScan { service: None }, 1, 20)
        .unwrap();
    assert_ne!(first, second);
    assert_eq!(
        controller.on_event(completed(7, first), 2).unwrap(),
        Update::Ignored
    );
    assert!(controller.is_busy());
}

#[test]
fn timeout_reports_once_and_late_completion_releases_request_slot() {
    let mut controller = Controller::new(MockBle::ready());
    let request = controller
        .submit(Command::StartScan { service: None }, 10, 5)
        .unwrap();
    assert_eq!(controller.next_deadline_ms(), Some(15));
    assert!(
        matches!(controller.advance(15).unwrap(), Update::Finished(outcome) if outcome.result == Err(Error::Timeout))
    );
    assert!(controller.is_busy());
    assert_eq!(
        controller
            .submit(Command::StopScan, 15, 5)
            .unwrap_err()
            .error,
        ControllerError::Device(Error::Busy)
    );
    assert!(matches!(
        controller.on_event(completed(7, request), 16).unwrap(),
        Update::Released(_)
    ));
    assert!(!controller.is_busy());
}

#[test]
fn provider_rejection_returns_command_and_consumes_request_id() {
    let mut provider = MockBle::ready();
    provider.reject_next = true;
    let last_attempt = Rc::clone(&provider.last_attempt);
    let mut controller = Controller::new(provider);
    let rejected = controller
        .submit(Command::StartScan { service: None }, 0, 5)
        .unwrap_err();
    assert_eq!(rejected.error, ControllerError::Device(Error::Busy));
    assert_eq!(rejected.value, Command::StartScan { service: None });
    let rejected_id = last_attempt.get().unwrap();
    let accepted = controller
        .submit(Command::StartScan { service: None }, 0, 5)
        .unwrap();
    assert_ne!(rejected_id, accepted);
}

#[test]
fn discovery_limit_is_forwarded_as_a_scan_event() {
    let mut controller = Controller::new(MockBle::ready());
    let event = Event {
        session: SessionId::new(7).unwrap(),
        kind: EventKind::DiscoveryLimited { limit: 32 },
    };
    assert_eq!(
        controller.on_event(event, 0).unwrap(),
        Update::Event(event.kind)
    );
}

#[test]
fn scan_events_do_not_reserve_gatt_value_storage() {
    assert!(core::mem::size_of::<Event>() <= 32);
    assert!(core::mem::size_of::<Command>() <= 20);
}

#[test]
fn lost_failure_is_reconciled_and_fault_survives_quiescence() {
    let device = MockBle::ready();
    let native_status = device.status.clone();
    let native_fault = device.fault.clone();
    let mut controller = Controller::new(device);
    native_fault.set(Some(Error::Io));
    native_status.set(ProviderStatus::Stopped);
    controller.reconcile().unwrap();
    assert_eq!(controller.status(), ProviderStatus::Stopped);
    assert_eq!(controller.fault(), Some(Error::Io));
    controller.finish().unwrap();
    assert_eq!(controller.fault(), Some(Error::Io));
}

#[test]
fn mismatched_completion_sets_sticky_fault_and_requires_quiescence() {
    let mut controller = Controller::new(MockBle::ready());
    let request = controller
        .submit(Command::StartScan { service: None }, 0, 20)
        .unwrap();
    let mismatch = Event {
        session: request.session,
        kind: EventKind::Completed {
            operation: request.operation,
            result: Ok(Completion::ScanStopped),
        },
    };
    assert_eq!(
        controller.on_event(mismatch, 1).unwrap(),
        Update::MismatchedCompletion(request)
    );
    assert_eq!(controller.fault(), Some(Error::Io));
    assert!(controller.is_busy());
    controller.request_stop().unwrap();
    assert!(
        matches!(controller.on_event(status(ProviderStatus::Stopped), 2).unwrap(), Update::Released(id) if id == request)
    );
    assert!(!controller.is_busy());
    assert_eq!(controller.fault(), Some(Error::Io));
}

#[test]
fn stop_requires_stopped_before_finish_and_status_cannot_resurrect() {
    let mut controller = Controller::new(MockBle::ready());
    assert_eq!(controller.finish(), Err(Error::Busy));
    controller.request_stop().unwrap();
    assert_eq!(controller.status(), ProviderStatus::Stopping);
    assert_eq!(
        controller
            .on_event(status(ProviderStatus::Stopped), 1)
            .unwrap(),
        Update::StatusChanged
    );
    assert_eq!(controller.finish(), Ok(()));
    assert_eq!(controller.finish(), Ok(()));
    assert_eq!(
        controller
            .on_event(status(ProviderStatus::Ready), 2)
            .unwrap(),
        Update::Ignored
    );
    assert_eq!(controller.status(), ProviderStatus::Stopped);
}

#[test]
fn fault_arriving_only_with_stopped_finishes_pending_request_as_failure() {
    let provider = MockBle::ready();
    let fault = provider.fault.clone();
    let mut controller = Controller::new(provider);
    let id = controller
        .submit(Command::StartScan { service: None }, 0, 20)
        .unwrap();
    fault.set(Some(Error::Io));
    assert_eq!(
        controller
            .on_event(status(ProviderStatus::Stopped), 1)
            .unwrap(),
        Update::Finished(ble::Outcome {
            id,
            result: Err(Error::Io),
        })
    );
    assert!(!controller.is_busy());
    assert_eq!(controller.fault(), Some(Error::Io));
}

#[test]
fn completion_at_deadline_times_out_and_clock_or_timeout_errors_do_not_admit() {
    let mut controller = Controller::new(MockBle::ready());
    assert_eq!(
        controller
            .submit(Command::StopScan, 0, 0)
            .unwrap_err()
            .error,
        ControllerError::InvalidTimeout
    );
    let id = controller
        .submit(Command::StartScan { service: None }, 10, 5)
        .unwrap();
    assert!(
        matches!(controller.on_event(completed(7, id), 15).unwrap(), Update::Finished(outcome) if outcome.result == Err(Error::Timeout))
    );
    assert_eq!(
        controller.advance(14).unwrap_err(),
        ControllerError::ClockWentBackwards
    );
    assert_eq!(
        controller
            .submit(Command::StopScan, u64::MAX, 1)
            .unwrap_err()
            .error,
        ControllerError::InvalidTimeout
    );
    assert!(!controller.is_busy());
}
