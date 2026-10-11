use ble::{
    Ble, Capabilities, Command, Completion, Controller, ControllerError, Error, Event, EventKind,
    PeerId, ProviderStatus, Rejected, Request, RequestId, ServiceId, SessionId, Update, Uuid,
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

impl<const N: usize> Ble<N> for MockBle {
    fn session(&self) -> SessionId {
        SessionId::new(7).unwrap()
    }

    fn capabilities(&self) -> Capabilities {
        Capabilities {
            central: true,
            scan: self.scan,
            max_value_len: N,
        }
    }

    fn status(&self) -> ProviderStatus {
        self.status.get()
    }

    fn fault(&self) -> Option<Error> {
        self.fault.get()
    }

    fn submit(&mut self, request: Request<N>) -> Result<(), Rejected<Request<N>, Error>> {
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

fn completion(session: u64, request: RequestId) -> Event<4> {
    Event {
        session: SessionId::new(session).unwrap(),
        kind: EventKind::Completed {
            operation: request.operation,
            result: Ok(Completion::ScanStarted),
        },
    }
}

fn status(status: ProviderStatus) -> Event<4> {
    Event {
        session: SessionId::new(7).unwrap(),
        kind: EventKind::Status(status),
    }
}

#[test]
fn lost_failure_event_is_recovered_even_after_quiescence() {
    let device = MockBle::ready();
    let native_status = device.status.clone();
    let native_fault = device.fault.clone();
    let mut controller = Controller::<_, 4>::new(device);
    native_fault.set(Some(Error::Io));
    native_status.set(ProviderStatus::Stopped);
    controller.reconcile().unwrap();
    assert_eq!(controller.status(), ProviderStatus::Stopped);
    assert_eq!(controller.fault(), Some(Error::Io));
    controller.finish().unwrap();
    assert_eq!(controller.fault(), Some(Error::Io));
}

#[test]
fn stale_session_and_operation_completions_do_not_finish_current_work() {
    let mut controller = Controller::<_, 4>::new(MockBle::ready());
    let first = controller
        .submit(Command::StartScan { service: None }, 0, 20)
        .unwrap();
    assert!(matches!(
        controller.on_event(completion(99, first), 1).unwrap(),
        Update::Ignored
    ));
    assert!(matches!(
        controller.on_event(completion(7, first), 1).unwrap(),
        Update::Finished(_)
    ));

    let second = controller
        .submit(Command::StartScan { service: None }, 1, 20)
        .unwrap();
    assert_ne!(first, second);
    assert!(matches!(
        controller.on_event(completion(7, first), 2).unwrap(),
        Update::Ignored
    ));
    assert!(controller.is_busy());
}

#[test]
fn advance_reports_timeout_once_but_late_native_completion_releases_slot() {
    let mut controller = Controller::<_, 4>::new(MockBle::ready());
    let request = controller
        .submit(Command::StartScan { service: None }, 10, 5)
        .unwrap();
    assert_eq!(controller.next_deadline_ms(), Some(15));
    assert!(matches!(
        controller.advance(15).unwrap(),
        Update::Finished(outcome) if outcome.result == Err(Error::Timeout)
    ));
    assert_eq!(controller.next_deadline_ms(), None);
    assert!(controller.is_busy());
    assert_eq!(
        controller
            .submit(Command::StopScan, 15, 5)
            .unwrap_err()
            .error,
        ControllerError::Device(Error::Busy)
    );
    assert!(matches!(
        controller.on_event(completion(7, request), 16).unwrap(),
        Update::Released(_)
    ));
    assert!(!controller.is_busy());
}

#[test]
fn late_completion_reports_timeout_even_without_advance() {
    let mut controller = Controller::<_, 4>::new(MockBle::ready());
    let request = controller
        .submit(Command::StartScan { service: None }, 10, 5)
        .unwrap();
    assert!(matches!(
        controller.on_event(completion(7, request), 16).unwrap(),
        Update::Finished(outcome)
            if outcome.id == request && outcome.result == Err(Error::Timeout)
    ));
    assert!(!controller.is_busy());
    assert_eq!(controller.next_deadline_ms(), None);
}

#[test]
fn provider_rejection_returns_command_and_consumes_operation_id() {
    let mut provider = MockBle::ready();
    provider.reject_next = true;
    let last_attempt = Rc::clone(&provider.last_attempt);
    let mut controller = Controller::<_, 4>::new(provider);
    let rejected = controller
        .submit(Command::StartScan { service: None }, 0, 5)
        .unwrap_err();
    assert_eq!(rejected.error, ControllerError::Device(Error::Busy));
    assert!(matches!(
        rejected.value,
        Command::StartScan { service: None }
    ));
    let rejected_id = last_attempt.get().unwrap();

    let accepted = controller
        .submit(Command::StartScan { service: None }, 0, 5)
        .unwrap();
    assert_ne!(rejected_id, accepted);
}

#[test]
fn scan_capability_is_independent_of_central_capability() {
    let mut provider = MockBle::ready();
    provider.scan = false;
    let mut controller = Controller::<_, 4>::new(provider);
    assert_eq!(
        controller
            .submit(Command::StartScan { service: None }, 0, 5)
            .unwrap_err()
            .error,
        ControllerError::Device(Error::Unsupported)
    );
    assert_eq!(
        controller
            .submit(Command::StopScan, 0, 5)
            .unwrap_err()
            .error,
        ControllerError::Device(Error::Unsupported)
    );
    assert!(controller.submit(Command::Connect(PeerId(1)), 0, 5).is_ok());
}

#[test]
fn reconcile_observes_sticky_provider_status() {
    let provider = MockBle::ready();
    let status_snapshot = Rc::clone(&provider.status);
    let mut controller = Controller::<_, 4>::new(provider);
    let request = controller
        .submit(Command::StartScan { service: None }, 0, 20)
        .unwrap();

    status_snapshot.set(ProviderStatus::Failed(Error::Io));
    assert!(matches!(
        controller.reconcile().unwrap(),
        Update::Finished(outcome)
            if outcome.id == request && outcome.result == Err(Error::Io)
    ));
    assert_eq!(controller.status(), ProviderStatus::Failed(Error::Io));
    assert_eq!(controller.fault(), Some(Error::Io));
    assert!(controller.is_busy());

    status_snapshot.set(ProviderStatus::Ready);
    assert_eq!(controller.reconcile().unwrap(), Update::Ignored);
    assert_eq!(controller.status(), ProviderStatus::Failed(Error::Io));
}

#[test]
fn mismatch_sets_sticky_io_and_keeps_slot_until_stopped() {
    let mut controller = Controller::<_, 4>::new(MockBle::ready());
    let request = controller
        .submit(Command::StartScan { service: None }, 0, 20)
        .unwrap();
    let mismatched = Event {
        session: request.session,
        kind: EventKind::Completed {
            operation: request.operation,
            result: Ok(Completion::Connected(PeerId(4))),
        },
    };
    assert_eq!(
        controller.on_event(mismatched, 1).unwrap(),
        Update::MismatchedCompletion(request)
    );
    assert_eq!(controller.fault(), Some(Error::Io));
    assert_eq!(controller.status(), ProviderStatus::Failed(Error::Io));
    assert!(controller.is_busy());
    assert_eq!(
        controller
            .submit(Command::StopScan, 1, 10)
            .unwrap_err()
            .error,
        ControllerError::Device(Error::Unavailable)
    );

    controller.request_stop().unwrap();
    assert_eq!(controller.status(), ProviderStatus::Stopping);
    assert_eq!(
        controller
            .on_event(status(ProviderStatus::Stopped), 2)
            .unwrap(),
        Update::Released(request)
    );
    assert!(!controller.is_busy());
    assert_eq!(controller.fault(), Some(Error::Io));
}

#[test]
fn wrong_peer_discovery_event_is_not_forwarded() {
    let mut controller = Controller::<_, 4>::new(MockBle::ready());
    let request = controller
        .submit(Command::DiscoverServices(PeerId(1)), 0, 20)
        .unwrap();
    let wrong_peer = Event {
        session: request.session,
        kind: EventKind::Service {
            operation: request.operation,
            peer: PeerId(2),
            service: ServiceId(3),
            uuid: Uuid([9; 16]),
        },
    };
    assert_eq!(controller.on_event(wrong_peer, 1).unwrap(), Update::Ignored);
    assert!(controller.is_busy());
}

#[test]
fn provider_failure_reports_error_but_retains_native_slot() {
    let mut controller = Controller::<_, 4>::new(MockBle::ready());
    let request = controller
        .submit(Command::StartScan { service: None }, 0, 20)
        .unwrap();
    assert!(matches!(
        controller
            .on_event(status(ProviderStatus::Failed(Error::Disconnected)), 1)
            .unwrap(),
        Update::Finished(outcome)
            if outcome.id == request && outcome.result == Err(Error::Disconnected)
    ));
    assert_eq!(controller.fault(), Some(Error::Disconnected));
    assert!(controller.is_busy());
    assert_eq!(
        controller
            .submit(Command::StopScan, 1, 10)
            .unwrap_err()
            .error,
        ControllerError::Device(Error::Unavailable)
    );

    controller.request_stop().unwrap();
    assert_eq!(
        controller
            .on_event(status(ProviderStatus::Stopped), 2)
            .unwrap(),
        Update::Released(request)
    );
    assert!(!controller.is_busy());
}

#[test]
fn stop_requires_stopped_before_finish_and_status_cannot_resurrect() {
    let mut controller = Controller::<_, 4>::new(MockBle::ready());
    assert_eq!(controller.finish(), Err(Error::Busy));
    controller.request_stop().unwrap();
    assert_eq!(controller.status(), ProviderStatus::Stopping);
    assert_eq!(
        controller
            .on_event(status(ProviderStatus::Stopped), 1)
            .unwrap(),
        Update::StatusChanged
    );
    assert_eq!(controller.status(), ProviderStatus::Stopped);
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
