use std::cell::{Cell, RefCell};
use std::rc::Rc;

use connectivity_service::{ReconnectError, Reconnector};
use wifi::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Action {
    Connect,
    Disconnect,
    Stop,
    Finish,
}

struct Shared {
    status: Cell<ProviderStatus>,
    fault: Cell<Option<Error>>,
    requests: RefCell<Vec<RequestId>>,
    commands: Cell<usize>,
    stop_calls: Cell<usize>,
    finish_calls: Cell<usize>,
    actions: RefCell<Vec<Action>>,
    reject: Cell<Option<Error>>,
    stop_error: Cell<Option<Error>>,
}

struct FakeWifi {
    session: SessionId,
    shared: Rc<Shared>,
}

impl Wifi for FakeWifi {
    fn session(&self) -> SessionId {
        self.session
    }

    fn capabilities(&self) -> Capabilities {
        Capabilities {
            scan: false,
            open_network: true,
            wpa2_personal: true,
            credential_storage: CredentialStorage::Volatile,
        }
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
        self.shared.actions.borrow_mut().push(match request.command {
            Command::Connect { .. } => Action::Connect,
            Command::Disconnect => Action::Disconnect,
            Command::Scan => panic!("unexpected scan"),
        });
        self.shared.requests.borrow_mut().push(request.id);
        self.shared.commands.set(self.shared.commands.get() + 1);
        Ok(())
    }

    fn request_stop(&mut self) -> Result<(), Error> {
        self.shared.stop_calls.set(self.shared.stop_calls.get() + 1);
        self.shared.actions.borrow_mut().push(Action::Stop);
        if let Some(error) = self.shared.stop_error.take() {
            return Err(error);
        }
        self.shared.status.set(ProviderStatus::Stopping);
        Ok(())
    }

    fn finish(&mut self) -> Result<(), Error> {
        assert_eq!(self.shared.status.get(), ProviderStatus::Stopped);
        self.shared.actions.borrow_mut().push(Action::Finish);
        self.shared
            .finish_calls
            .set(self.shared.finish_calls.get() + 1);
        Ok(())
    }
}

struct Fixture {
    session: SessionId,
    shared: Rc<Shared>,
    service: Reconnector<FakeWifi, fn() -> Result<Command, Error>>,
}

fn fresh_connect() -> Result<Command, Error> {
    Ok(Command::Connect {
        ssid: Ssid::new(b"fixture").unwrap(),
        credentials: Credentials::Open,
    })
}

impl Fixture {
    fn new() -> Self {
        Self::with_status(ProviderStatus::Ready)
    }

    fn with_status(status: ProviderStatus) -> Self {
        let session = SessionId::new(1).unwrap();
        let shared = Rc::new(Shared {
            status: Cell::new(status),
            fault: Cell::new(None),
            requests: RefCell::new(Vec::new()),
            commands: Cell::new(0),
            stop_calls: Cell::new(0),
            finish_calls: Cell::new(0),
            actions: RefCell::new(Vec::new()),
            reject: Cell::new(None),
            stop_error: Cell::new(None),
        });
        let controller = Controller::new(FakeWifi {
            session,
            shared: shared.clone(),
        });
        let service = Reconnector::new(controller, fresh_connect as fn() -> Result<Command, Error>);
        Self {
            session,
            shared,
            service,
        }
    }

    fn joined() -> Self {
        let mut fixture = Self::new();
        fixture.service.poll(1).unwrap();
        fixture.addressed(2);
        fixture.complete(0, Ok(Completion::Connected), 3);
        fixture
    }

    fn event(&mut self, kind: EventKind, now_ms: u64) -> Result<(), ReconnectError> {
        self.service.on_event(
            Event {
                session: self.session,
                kind,
            },
            now_ms,
        )
    }

    fn complete(&mut self, index: usize, result: Result<Completion, Error>, now_ms: u64) {
        self.complete_result(index, result, now_ms).unwrap();
    }

    fn complete_result(
        &mut self,
        index: usize,
        result: Result<Completion, Error>,
        now_ms: u64,
    ) -> Result<(), ReconnectError> {
        let id = self.shared.requests.borrow()[index];
        self.event(
            EventKind::Completed {
                operation: id.operation,
                result,
            },
            now_ms,
        )
    }

    fn addressed(&mut self, now_ms: u64) {
        self.event(EventKind::LinkChanged(LinkState::Addressed), now_ms)
            .unwrap();
    }

    fn disconnected(&mut self, now_ms: u64) {
        self.event(EventKind::LinkChanged(LinkState::Disconnected), now_ms)
            .unwrap();
    }

    fn stopped(&mut self, now_ms: u64) -> Result<(), ReconnectError> {
        self.shared.status.set(ProviderStatus::Stopped);
        self.event(EventKind::Status(ProviderStatus::Stopped), now_ms)
    }
}

#[test]
fn initial_poll_starts_one_join() {
    let mut f = Fixture::new();
    f.service.poll(1).unwrap();
    assert_eq!(f.shared.commands.get(), 1);
    assert_eq!(f.service.stats().attempts, 1);
    assert!(f.service.link() != LinkState::Addressed);
}

#[test]
fn link_loss_waits_for_backoff_then_rejoins() {
    let mut f = Fixture::new();
    f.service.poll(1).unwrap();
    f.complete(0, Ok(Completion::Connected), 2);
    f.addressed(3);
    f.disconnected(4);
    assert_eq!(f.service.stats().link_losses, 1);
    assert_eq!(f.service.next_deadline_ms(), Some(254));
    f.service.poll(253).unwrap();
    assert_eq!(f.shared.commands.get(), 1);
    f.service.poll(254).unwrap();
    assert_eq!(f.shared.commands.get(), 2);
    f.complete(1, Ok(Completion::Connected), 255);
    f.addressed(256);
    assert_eq!(f.service.stats().reconnections, 1);
}

#[test]
fn timed_out_operation_does_not_overlap_retry_before_native_release() {
    let mut f = Fixture::new();
    f.service.poll(1).unwrap();
    f.service.poll(30_001).unwrap();
    assert_eq!(f.service.next_deadline_ms(), None);
    assert_eq!(f.service.stats().attempts, 1);
    assert_eq!(f.shared.commands.get(), 1);
    f.complete(0, Err(Error::Timeout), 30_002);
    f.service.poll(30_251).unwrap();
    assert_eq!(f.shared.commands.get(), 2);
}

#[test]
fn authentication_failure_is_terminal_and_visible() {
    let mut f = Fixture::new();
    f.service.poll(1).unwrap();
    let operation = f.shared.requests.borrow()[0].operation;
    assert_eq!(
        f.event(
            EventKind::Completed {
                operation,
                result: Err(Error::AuthenticationFailed),
            },
            2,
        ),
        Err(ReconnectError::Terminal(Error::AuthenticationFailed))
    );
    assert_eq!(
        f.service.terminal_error(),
        Some(Error::AuthenticationFailed)
    );
    assert_eq!(
        f.service.poll(3),
        Err(ReconnectError::Terminal(Error::AuthenticationFailed))
    );
    assert_eq!(f.shared.commands.get(), 1);
    f.service.request_stop().unwrap();
    assert_eq!(f.shared.stop_calls.get(), 1);
}

#[test]
fn manual_reconnect_waits_for_disconnect_completion() {
    let mut f = Fixture::new();
    f.service.poll(1).unwrap();
    f.complete(0, Ok(Completion::Connected), 2);
    f.addressed(3);
    f.service.request_reconnect(4).unwrap();
    f.service.poll(4).unwrap();
    assert_eq!(f.shared.commands.get(), 2);
    f.service.poll(5).unwrap();
    assert_eq!(f.shared.commands.get(), 2);
    f.complete(1, Ok(Completion::Disconnected), 6);
    f.service.poll(6).unwrap();
    assert_eq!(f.shared.commands.get(), 3);
    f.addressed(7);
    f.complete(2, Ok(Completion::Connected), 8);
    f.service.poll(9).unwrap();
    assert_eq!(
        f.shared.commands.get(),
        3,
        "manual request must not repeat after recovery"
    );
}

#[test]
fn manual_reconnect_failure_also_obeys_backoff() {
    let mut f = Fixture::new();
    f.service.poll(1).unwrap();
    f.addressed(2);
    f.complete(0, Ok(Completion::Connected), 3);
    f.service.request_reconnect(4).unwrap();
    f.service.poll(4).unwrap();
    f.disconnected(5);
    f.complete(1, Err(Error::Io), 6);
    f.service.poll(255).unwrap();
    assert_eq!(f.shared.commands.get(), 2);
    f.service.poll(256).unwrap();
    assert_eq!(f.shared.commands.get(), 3);
}

#[test]
fn stop_then_stopped_event_is_required_before_finish() {
    let mut f = Fixture::new();
    f.service.request_stop().unwrap();
    assert_eq!(f.service.status(), ProviderStatus::Stopping);
    assert_eq!(f.service.finish(), Err(ReconnectError::Device(Error::Busy)));
    f.shared.status.set(ProviderStatus::Stopped);
    f.event(EventKind::Status(ProviderStatus::Stopped), 1)
        .unwrap();
    f.service.finish().unwrap();
    assert_eq!(f.shared.finish_calls.get(), 1);
}

#[test]
fn disconnect_and_stop_waits_for_completion_and_quiescence_before_joining() {
    let mut f = Fixture::joined();
    f.service.request_reconnect(4).unwrap();
    f.service.poll(4).unwrap();
    f.service.request_disconnect_and_stop(4).unwrap();
    assert_eq!(f.shared.actions.borrow().as_slice(), &[Action::Connect, Action::Disconnect]);
    assert_eq!(f.shared.stop_calls.get(), 0);
    f.service.poll(5).unwrap();
    f.service.request_disconnect_and_stop(5).unwrap();
    assert_eq!(f.shared.commands.get(), 2, "disconnect is submitted only once");
    assert_eq!(f.service.finish(), Err(ReconnectError::Device(Error::Busy)));
    assert_eq!(f.shared.finish_calls.get(), 0);

    // Completion is authoritative even before the link snapshot catches up.
    f.complete(1, Ok(Completion::Disconnected), 6);
    assert_eq!(f.shared.stop_calls.get(), 0, "callbacks do not stop the provider");
    f.service.poll(6).unwrap();
    assert_eq!(f.service.status(), ProviderStatus::Stopping);
    assert_eq!(f.service.finish(), Err(ReconnectError::Device(Error::Busy)));
    assert_eq!(f.shared.finish_calls.get(), 0);
    f.stopped(7).unwrap();
    f.service.finish().unwrap();
    f.service.finish().unwrap();
    f.service.poll(8).unwrap();
    assert_eq!(
        f.shared.actions.borrow().as_slice(),
        &[Action::Connect, Action::Disconnect, Action::Stop, Action::Finish]
    );
    assert_eq!(f.service.stats().attempts, 1, "shutdown never rejoins");
}

#[test]
fn disconnect_failure_is_retained_through_stop_and_join() {
    let mut f = Fixture::joined();
    f.service.request_disconnect_and_stop(4).unwrap();
    let failure = ReconnectError::Device(Error::Io);
    assert_eq!(f.complete_result(1, Err(Error::Io), 5), Err(failure));
    assert_eq!(f.shared.stop_calls.get(), 0);
    assert_eq!(f.service.poll(5), Err(failure));
    assert_eq!(f.service.status(), ProviderStatus::Stopping);
    assert_eq!(f.service.shutdown_error(), Some(failure));
    assert_eq!(f.service.finish(), Err(ReconnectError::Device(Error::Busy)));
    assert_eq!(f.shared.finish_calls.get(), 0);
    assert_eq!(f.stopped(6), Err(failure));
    assert_eq!(f.service.finish(), Err(failure));
    assert_eq!(f.shared.finish_calls.get(), 1, "join precedes reporting failure");
    assert_eq!(f.service.poll(7), Err(failure));
    assert_eq!(f.service.finish(), Err(failure));
    assert_eq!(f.shared.finish_calls.get(), 1);
    assert_eq!(
        f.shared.actions.borrow().as_slice(),
        &[Action::Connect, Action::Disconnect, Action::Stop, Action::Finish]
    );
}

#[test]
fn rejected_shutdown_disconnect_still_stops_without_retrying() {
    for error in [Error::Busy, Error::Io] {
        let mut f = Fixture::joined();
        f.shared.reject.set(Some(error));
        let failure = ReconnectError::Device(error);
        assert_eq!(f.service.request_disconnect_and_stop(4), Err(failure));
        assert_eq!(f.service.status(), ProviderStatus::Stopping);
        assert_eq!(f.service.poll(5), Err(failure));
        assert_eq!(f.stopped(6), Err(failure));
        assert_eq!(f.service.finish(), Err(failure));
        assert_eq!(
            f.shared.actions.borrow().as_slice(),
            &[Action::Connect, Action::Stop, Action::Finish]
        );
    }
}

#[test]
fn shutdown_waits_for_the_native_slot_but_is_bounded_to_thirty_seconds() {
    let mut f = Fixture::new();
    f.service.poll(1).unwrap();
    f.addressed(2);
    f.service.request_disconnect_and_stop(3).unwrap();
    assert_eq!(f.shared.commands.get(), 1, "join still owns the native slot");
    assert_eq!(f.shared.stop_calls.get(), 0);
    f.complete(0, Ok(Completion::Connected), 4);
    f.service.poll(4).unwrap();
    assert_eq!(f.shared.commands.get(), 2);
    assert_eq!(f.service.next_deadline_ms(), Some(30_003));
    f.service.poll(30_002).unwrap();
    assert_eq!(f.shared.stop_calls.get(), 0);
    let failure = ReconnectError::Device(Error::Timeout);
    // Stop is independent of the still-pending disconnect slot.
    assert_eq!(f.service.poll(30_003), Err(failure));
    assert_eq!(f.shared.stop_calls.get(), 1);
    assert_eq!(f.service.status(), ProviderStatus::Stopping);
    assert_eq!(f.stopped(30_004), Err(failure));
    assert_eq!(f.service.finish(), Err(failure));
    assert_eq!(f.shared.finish_calls.get(), 1);
    assert_eq!(f.shared.commands.get(), 2);
}

#[test]
fn starting_and_failed_shutdown_stop_without_waiting_for_ready() {
    for (status, expected) in [
        (ProviderStatus::Starting, Ok(())),
        (ProviderStatus::Failed(Error::Io), Err(ReconnectError::ProviderFault(Error::Io))),
    ] {
        let mut f = Fixture::with_status(status);
        assert_eq!(f.service.request_disconnect_and_stop(1), expected);
        assert_eq!(f.service.status(), ProviderStatus::Stopping);
        assert_eq!(f.shared.commands.get(), 0);
        assert_eq!(f.shared.stop_calls.get(), 1);
        assert_eq!(f.stopped(2), expected);
        assert_eq!(f.service.finish(), expected);
        assert_eq!(f.shared.finish_calls.get(), 1);
    }
}

#[test]
fn sticky_terminal_error_does_not_block_shutdown_progress() {
    let mut f = Fixture::new();
    f.service.poll(1).unwrap();
    let failure = ReconnectError::Terminal(Error::AuthenticationFailed);
    assert_eq!(f.complete_result(0, Err(Error::AuthenticationFailed), 2), Err(failure));
    // A late link snapshot remains authoritative despite the policy failure.
    assert_eq!(f.event(EventKind::LinkChanged(LinkState::Associated), 3), Err(failure));
    assert_eq!(f.service.request_disconnect_and_stop(4), Err(failure));
    assert_eq!(f.shared.commands.get(), 2);
    assert_eq!(f.complete_result(1, Ok(Completion::Disconnected), 5), Err(failure));
    assert_eq!(f.service.poll(5), Err(failure));
    assert_eq!(f.service.status(), ProviderStatus::Stopping);
    assert_eq!(f.stopped(6), Err(failure));
    assert_eq!(f.service.finish(), Err(failure));
    assert_eq!(f.shared.finish_calls.get(), 1);
}

#[test]
fn provider_fault_during_disconnect_still_reaches_stop_and_join() {
    let mut f = Fixture::joined();
    f.service.request_disconnect_and_stop(4).unwrap();
    f.shared.status.set(ProviderStatus::Failed(Error::Protocol));
    let failure = ReconnectError::ProviderFault(Error::Protocol);
    // Reconciliation must not return early on the failed pending operation.
    assert_eq!(f.service.poll(5), Err(failure));
    assert_eq!(f.service.status(), ProviderStatus::Stopping);
    assert_eq!(f.shared.stop_calls.get(), 1);
    assert_eq!(f.stopped(6), Err(failure));
    assert_eq!(f.service.finish(), Err(failure));
    assert_eq!(f.shared.finish_calls.get(), 1);
}

#[test]
fn stop_failure_is_retained_while_later_poll_retries_provider_stop() {
    let mut f = Fixture::new();
    f.shared.stop_error.set(Some(Error::Io));
    let failure = ReconnectError::Device(Error::Io);
    assert_eq!(f.service.request_disconnect_and_stop(1), Err(failure));
    assert_eq!(f.service.status(), ProviderStatus::Ready);
    assert_eq!(f.service.poll(2), Err(failure));
    assert_eq!(f.service.status(), ProviderStatus::Stopping);
    assert_eq!(f.shared.stop_calls.get(), 2);
    assert_eq!(f.shared.commands.get(), 0);
    assert_eq!(f.stopped(3), Err(failure));
    assert_eq!(f.service.finish(), Err(failure));
    assert_eq!(f.shared.finish_calls.get(), 1);
}
