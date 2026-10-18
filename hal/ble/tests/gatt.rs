use ble::{
    gatt::{
        Attribute, Client, Command, Completion, Driver, Event, EventKind, Failure, Peer, Reply,
        Request, State, Update, Value,
    },
    ControllerError, Error, PeerId, Rejected, RequestId, SessionId, Uuid,
};
use core::num::NonZeroU16;
use std::{cell::RefCell, collections::VecDeque, rc::Rc};

const SESSION: u64 = 7;
const EVENT_CAPACITY: usize = 16;
const VALUE_CAPACITY: usize = 8;
type TestClient = Client<FakeDriver<VALUE_CAPACITY>, VALUE_CAPACITY>;

#[derive(Default)]
struct Shared<const N: usize> {
    attempts: Vec<Request<N>>,
    accepted: Vec<Request<N>>,
    recoveries: Vec<RequestId>,
    events: VecDeque<Event<N>>,
    reject_next: Option<Error>,
}

#[derive(Clone)]
struct FakeHandle<const N: usize>(Rc<RefCell<Shared<N>>>);

struct FakeDriver<const N: usize> {
    shared: Rc<RefCell<Shared<N>>>,
}

impl<const N: usize> FakeDriver<N> {
    fn new() -> (Self, FakeHandle<N>) {
        let shared = Rc::new(RefCell::new(Shared::default()));
        (
            Self {
                shared: shared.clone(),
            },
            FakeHandle(shared),
        )
    }
}

impl<const N: usize> FakeHandle<N> {
    fn attempts(&self) -> Vec<Request<N>> {
        self.0.borrow().attempts.clone()
    }

    fn accepted(&self) -> Vec<Request<N>> {
        self.0.borrow().accepted.clone()
    }

    fn recoveries(&self) -> Vec<RequestId> {
        self.0.borrow().recoveries.clone()
    }

    fn reject_next(&self, error: Error) {
        self.0.borrow_mut().reject_next = Some(error);
    }

    fn push(&self, event: Event<N>) {
        let mut shared = self.0.borrow_mut();
        assert!(
            shared.events.len() < EVENT_CAPACITY,
            "test event queue is full"
        );
        shared.events.push_back(event);
    }
}

impl<const N: usize> Driver<N> for FakeDriver<N> {
    fn session(&self) -> SessionId {
        SessionId::new(SESSION).unwrap()
    }

    fn submit(&mut self, request: Request<N>) -> Result<(), Rejected<Request<N>, Error>> {
        let mut shared = self.shared.borrow_mut();
        shared.attempts.push(request);
        if matches!(request.command, Command::Connect { peer } if peer.session != SessionId::new(SESSION).unwrap())
        {
            return Err(Rejected {
                value: request,
                error: Error::InvalidValue,
            });
        }
        if let Some(error) = shared.reject_next.take() {
            return Err(Rejected {
                value: request,
                error,
            });
        }
        shared.accepted.push(request);
        Ok(())
    }

    fn recover(&mut self, token: RequestId) {
        self.shared.borrow_mut().recoveries.push(token);
    }

    fn poll(&mut self) -> Option<Event<N>> {
        self.shared.borrow_mut().events.pop_front()
    }
}

fn new_client() -> (TestClient, FakeHandle<VALUE_CAPACITY>) {
    let (driver, handle) = FakeDriver::new();
    (Client::new(driver, 10).unwrap(), handle)
}

fn event(kind: EventKind<VALUE_CAPACITY>) -> Event<VALUE_CAPACITY> {
    event_in(SESSION, kind)
}

fn event_in(session: u64, kind: EventKind<VALUE_CAPACITY>) -> Event<VALUE_CAPACITY> {
    Event {
        session: SessionId::new(session).unwrap(),
        kind,
    }
}

fn completed(
    id: RequestId,
    result: Result<Reply<VALUE_CAPACITY>, Failure>,
) -> Event<VALUE_CAPACITY> {
    event(EventKind::Completed {
        request: id,
        result,
    })
}

fn peer(id: u64) -> Peer {
    Peer {
        session: SessionId::new(SESSION).unwrap(),
        id: PeerId(id),
    }
}

fn connect(
    client: &mut TestClient,
    handle: &FakeHandle<VALUE_CAPACITY>,
    now: u64,
) -> ble::gatt::LinkId {
    let id = client
        .submit(Command::Connect { peer: peer(42) }, now, 50)
        .unwrap();
    handle.push(completed(id, Ok(Reply::Connected)));
    match client.poll(now + 1).unwrap() {
        Update::Finished(outcome) => match outcome.result.unwrap() {
            Completion::Connected(link) => link,
            other => panic!("unexpected connect result: {other:?}"),
        },
        other => panic!("unexpected connect update: {other:?}"),
    }
}

fn uuid(byte: u8) -> Uuid {
    Uuid([byte; 16])
}

fn attr(handle: FakeHandle<VALUE_CAPACITY>, client: &mut TestClient, now: u64) -> Attribute {
    let service = uuid(1);
    let characteristic = uuid(2);
    let id = client
        .submit(
            Command::Discover {
                service,
                characteristic,
            },
            now,
            50,
        )
        .unwrap();
    assert_eq!(
        handle.accepted().last().unwrap().command,
        Command::Discover {
            service,
            characteristic
        }
    );
    handle.push(completed(
        id,
        Ok(Reply::Discovered {
            handle: NonZeroU16::new(23).unwrap(),
        }),
    ));
    match client.poll(now + 1).unwrap() {
        Update::Finished(outcome) => match outcome.result.unwrap() {
            Completion::Discovered(attribute) => attribute,
            other => panic!("unexpected discovery result: {other:?}"),
        },
        other => panic!("unexpected discovery update: {other:?}"),
    }
}

#[test]
fn connect_discover_read_acknowledged_write_and_disconnect_succeed() {
    let (mut client, driver) = new_client();
    let link = connect(&mut client, &driver, 0);
    assert_eq!(client.state(), State::Connected(link));

    let attribute = attr(driver.clone(), &mut client, 2);
    assert_eq!(attribute.link(), link);
    assert_eq!(attribute.handle().get(), 23);

    let read = client.submit(Command::Read { attribute }, 4, 50).unwrap();
    let value = Value::new(&[1, 2, 3]).unwrap();
    driver.push(completed(read, Ok(Reply::Read(value))));
    assert!(matches!(client.poll(5).unwrap(), Update::Finished(outcome)
        if outcome.id == read && outcome.result == Ok(Completion::Read(value))));

    let write_value = Value::new(&[9, 8]).unwrap();
    let write = client
        .submit(
            Command::Write {
                attribute,
                value: write_value,
            },
            6,
            50,
        )
        .unwrap();
    assert_eq!(
        driver.accepted().last().unwrap().command,
        Command::Write {
            attribute,
            value: write_value
        }
    );
    driver.push(completed(write, Ok(Reply::Written)));
    assert!(matches!(client.poll(7).unwrap(), Update::Finished(outcome)
        if outcome.id == write && outcome.result == Ok(Completion::Written)));

    let disconnect = client.submit(Command::Disconnect, 8, 50).unwrap();
    driver.push(completed(disconnect, Ok(Reply::Disconnected)));
    assert!(matches!(client.poll(9).unwrap(), Update::Finished(outcome)
        if outcome.id == disconnect && outcome.result == Ok(Completion::Disconnected)));
    assert_eq!(client.state(), State::Idle);
    assert!(driver.recoveries().is_empty());
}

#[test]
fn native_rejection_returns_owned_command_and_consumes_id() {
    let (mut client, driver) = new_client();
    let command = Command::Connect { peer: peer(9) };
    driver.reject_next(Error::Busy);
    let rejected = client.submit(command, 0, 20).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::Device(Error::Busy));
    let rejected_id = driver.attempts()[0].id;
    let accepted_id = client.submit(command, 0, 20).unwrap();
    assert_ne!(rejected_id, accepted_id);
    assert_eq!(accepted_id.operation, ble::OperationId::new(2).unwrap());
}

#[test]
fn connect_rejects_peer_from_stale_scan_session_and_returns_it() {
    let (mut client, driver) = new_client();
    let command = Command::Connect {
        peer: Peer {
            session: SessionId::new(SESSION + 1).unwrap(),
            id: PeerId(1),
        },
    };
    let rejected = client.submit(command, 0, 20).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::Device(Error::InvalidValue));
    assert_eq!(driver.attempts().len(), 1);
    assert!(driver.accepted().is_empty());
}

#[test]
fn zero_and_overflowing_deadlines_return_the_original_command() {
    let command = Command::Connect { peer: peer(1) };
    let (mut client, _) = new_client();
    let rejected = client.submit(command, 0, 0).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::InvalidTimeout);

    let (mut client, _) = new_client();
    let rejected = client.submit(command, u64::MAX - 5, 10).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::InvalidTimeout);

    // The command deadline fits, but reserving its recovery deadline does not.
    let (mut client, _) = new_client();
    let rejected = client.submit(command, u64::MAX - 10, 5).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::InvalidTimeout);
}

#[test]
fn backwards_clock_is_rejected_without_losing_command_or_pending_request() {
    let (mut client, _) = new_client();
    let command = Command::Connect { peer: peer(1) };
    client.submit(command, 10, 20).unwrap();
    let rejected = client.submit(command, 9, 20).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::ClockWentBackwards);
    assert_eq!(client.state(), State::Connecting);
}

#[test]
fn one_operation_may_be_in_flight() {
    let (mut client, _) = new_client();
    let command = Command::Connect { peer: peer(1) };
    client.submit(command, 0, 20).unwrap();
    let rejected = client.submit(command, 1, 20).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::Device(Error::Busy));
}

#[test]
fn timeout_at_exact_boundary_recovers_once_and_waits_for_matching_barrier() {
    let (mut client, driver) = new_client();
    let id = client
        .submit(Command::Connect { peer: peer(1) }, 10, 5)
        .unwrap();
    assert_eq!(client.next_deadline_ms(), Some(15));
    assert!(matches!(client.poll(14).unwrap(), Update::None));
    assert!(matches!(client.poll(15).unwrap(), Update::Finished(outcome)
        if outcome.id == id && outcome.result == Err(Failure::Device(Error::Timeout))));
    assert_eq!(driver.recoveries(), vec![id]);
    assert_eq!(client.poll(15).unwrap(), Update::None);

    driver.push(completed(id, Ok(Reply::Connected)));
    assert_eq!(client.poll(16).unwrap(), Update::Ignored);
    assert_eq!(client.state(), State::Recovering);

    let wrong_token = RequestId {
        session: id.session,
        operation: ble::OperationId::new(99).unwrap(),
    };
    driver.push(event(EventKind::Quiescent {
        recovery: wrong_token,
    }));
    assert_eq!(client.poll(17).unwrap(), Update::Ignored);
    assert_eq!(client.state(), State::Recovering);
    driver.push(event(EventKind::Quiescent { recovery: id }));
    assert_eq!(client.poll(18).unwrap(), Update::Recovered);
    assert_eq!(client.state(), State::Idle);
}

#[test]
fn disconnect_during_pending_operation_fails_immediately_and_requires_barrier() {
    let (mut client, driver) = new_client();
    let link = connect(&mut client, &driver, 0);
    let attribute = attr(driver.clone(), &mut client, 2);
    assert_eq!(attribute.link(), link);
    let read = client.submit(Command::Read { attribute }, 4, 40).unwrap();
    driver.push(event(EventKind::Disconnected {
        connection: link.request(),
        reason: Some(19),
    }));
    assert!(matches!(client.poll(5).unwrap(), Update::Finished(outcome)
        if outcome.id == read && outcome.result == Err(Failure::Disconnected)));
    assert_eq!(driver.recoveries(), vec![read]);
    assert_eq!(client.state(), State::Recovering);
    assert_eq!(client.poll(6).unwrap(), Update::None);
    driver.push(event(EventKind::Quiescent { recovery: read }));
    assert_eq!(client.poll(7).unwrap(), Update::Recovered);
}

#[test]
fn disconnect_during_connect_fails_the_connect_and_recovers_its_request() {
    let (mut client, driver) = new_client();
    let connect_id = client
        .submit(Command::Connect { peer: peer(1) }, 1, 40)
        .unwrap();
    driver.push(event(EventKind::Disconnected {
        connection: connect_id,
        reason: Some(8),
    }));
    assert!(matches!(client.poll(2).unwrap(), Update::Finished(outcome)
        if outcome.id == connect_id && outcome.result == Err(Failure::Disconnected)));
    assert_eq!(driver.recoveries(), vec![connect_id]);
    assert_eq!(client.state(), State::Recovering);
    driver.push(event(EventKind::Quiescent {
        recovery: connect_id,
    }));
    assert_eq!(client.poll(3).unwrap(), Update::Recovered);
}

#[test]
fn idle_disconnect_reports_link_and_uses_link_request_as_recovery_token() {
    let (mut client, driver) = new_client();
    let link = connect(&mut client, &driver, 0);
    driver.push(event(EventKind::Disconnected {
        connection: link.request(),
        reason: None,
    }));
    assert_eq!(
        client.poll(2).unwrap(),
        Update::Disconnected { link, reason: None }
    );
    assert_eq!(driver.recoveries(), vec![link.request()]);
    assert_eq!(client.state(), State::Recovering);
    driver.push(event(EventKind::Quiescent {
        recovery: link.request(),
    }));
    assert_eq!(client.poll(3).unwrap(), Update::Recovered);
}

#[test]
fn requested_disconnect_accepts_link_notification_before_terminal_barrier() {
    let (mut client, driver) = new_client();
    let link = connect(&mut client, &driver, 0);
    let id = client.submit(Command::Disconnect, 2, 40).unwrap();
    assert_eq!(client.state(), State::Disconnecting);
    driver.push(event(EventKind::Disconnected {
        connection: link.request(),
        reason: Some(22),
    }));
    assert_eq!(
        client.poll(3).unwrap(),
        Update::Disconnected {
            link,
            reason: Some(22)
        }
    );
    assert_eq!(client.state(), State::Disconnecting);
    assert!(client.is_busy());
    assert_eq!(client.next_deadline_ms(), Some(42));
    assert!(driver.recoveries().is_empty());
    driver.push(completed(id, Ok(Reply::Disconnected)));
    assert!(matches!(client.poll(4).unwrap(), Update::Finished(outcome)
        if outcome.id == id && outcome.result == Ok(Completion::Disconnected)));
    assert_eq!(client.state(), State::Idle);
    assert!(driver.recoveries().is_empty());
}

#[test]
fn disconnect_notification_without_barrier_still_times_out_and_drains() {
    let (mut client, driver) = new_client();
    let link = connect(&mut client, &driver, 0);
    let id = client.submit(Command::Disconnect, 2, 3).unwrap();
    driver.push(event(EventKind::Disconnected {
        connection: link.request(),
        reason: None,
    }));
    assert!(matches!(
        client.poll(3).unwrap(),
        Update::Disconnected { .. }
    ));
    assert!(matches!(client.poll(5).unwrap(), Update::Finished(outcome)
        if outcome.id == id && outcome.result == Err(Failure::Device(Error::Timeout))));
    assert_eq!(client.state(), State::Recovering);
    assert_eq!(driver.recoveries(), vec![id]);
    driver.push(completed(id, Ok(Reply::Disconnected)));
    assert_eq!(client.poll(6).unwrap(), Update::Ignored);
    assert!(client.is_busy());
    driver.push(event(EventKind::Quiescent { recovery: id }));
    assert_eq!(client.poll(7).unwrap(), Update::Recovered);
    assert_eq!(client.state(), State::Idle);
}

#[test]
fn wrong_session_and_operation_completions_are_ignored() {
    let (mut client, driver) = new_client();
    let id = client
        .submit(Command::Connect { peer: peer(1) }, 0, 40)
        .unwrap();
    driver.push(event_in(
        SESSION + 1,
        EventKind::Completed {
            request: id,
            result: Ok(Reply::Connected),
        },
    ));
    let other = RequestId {
        session: id.session,
        operation: ble::OperationId::new(99).unwrap(),
    };
    driver.push(completed(other, Ok(Reply::Connected)));
    assert_eq!(client.poll(1).unwrap(), Update::Ignored);
    assert_eq!(client.poll(2).unwrap(), Update::Ignored);
    assert_eq!(client.state(), State::Connecting);
    driver.push(completed(id, Ok(Reply::Connected)));
    assert!(matches!(client.poll(3).unwrap(), Update::Finished(_)));
}

#[test]
fn coalesced_wakeup_drains_stale_events_before_waiting() {
    let (mut client, driver) = new_client();
    let id = client
        .submit(Command::Connect { peer: peer(1) }, 0, 40)
        .unwrap();
    driver.push(event_in(
        SESSION + 1,
        EventKind::Completed {
            request: id,
            result: Ok(Reply::Connected),
        },
    ));
    driver.push(completed(id, Ok(Reply::Connected)));
    let mut finished = 0;
    // One wakeup for both records. Only None permits returning to the wait.
    loop {
        match client.poll(1).unwrap() {
            Update::None => break,
            Update::Ignored => (),
            Update::Finished(_) => finished += 1,
            other => panic!("unexpected update: {other:?}"),
        }
    }
    assert_eq!(finished, 1);
    assert!(matches!(client.state(), State::Connected(_)));
}

#[test]
fn value_capacity_is_bounded_and_does_not_expand_scan_events_or_client_bookkeeping() {
    assert!(Value::<8>::new(&[0; 9]).is_none());
    assert!(Value::<0>::new(&[]).is_some());
    assert_eq!(
        core::mem::size_of::<Client<FakeDriver<0>, 0>>(),
        core::mem::size_of::<Client<FakeDriver<512>, 512>>(),
    );
    assert!(core::mem::size_of::<ble::Event>() <= 32);
}

#[test]
fn disconnect_for_another_connection_is_ignored() {
    let (mut client, driver) = new_client();
    let link = connect(&mut client, &driver, 0);
    driver.push(event(EventKind::Disconnected {
        connection: RequestId {
            session: link.request().session,
            operation: ble::OperationId::new(90).unwrap(),
        },
        reason: Some(4),
    }));
    assert_eq!(client.poll(2).unwrap(), Update::Ignored);
    assert_eq!(client.state(), State::Connected(link));
    assert!(driver.recoveries().is_empty());
}

#[test]
fn attributes_from_a_disconnected_link_are_rejected_after_reconnect() {
    let (mut client, driver) = new_client();
    let first_link = connect(&mut client, &driver, 0);
    let old_attribute = attr(driver.clone(), &mut client, 2);
    let disconnect = client.submit(Command::Disconnect, 4, 40).unwrap();
    driver.push(completed(disconnect, Ok(Reply::Disconnected)));
    assert!(matches!(client.poll(5).unwrap(), Update::Finished(_)));

    let second_link = connect(&mut client, &driver, 6);
    assert_ne!(first_link, second_link);
    let command = Command::Read {
        attribute: old_attribute,
    };
    let rejected = client.submit(command, 8, 40).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::Device(Error::InvalidValue));
}

#[test]
fn remote_att_failures_during_discovery_read_and_write_preserve_the_link() {
    let (mut client, driver) = new_client();
    let link = connect(&mut client, &driver, 0);

    let discover = client
        .submit(
            Command::Discover {
                service: uuid(1),
                characteristic: uuid(2),
            },
            2,
            40,
        )
        .unwrap();
    driver.push(completed(discover, Err(Failure::Att(0x0e))));
    assert!(matches!(client.poll(3).unwrap(), Update::Finished(outcome)
        if outcome.id == discover && outcome.result == Err(Failure::Att(0x0e))));
    assert_eq!(client.state(), State::Connected(link));

    let attribute = attr(driver.clone(), &mut client, 4);
    let read = client.submit(Command::Read { attribute }, 6, 40).unwrap();
    driver.push(completed(read, Err(Failure::Att(0x02))));
    assert!(matches!(client.poll(7).unwrap(), Update::Finished(outcome)
        if outcome.id == read && outcome.result == Err(Failure::Att(0x02))));
    assert_eq!(client.state(), State::Connected(link));

    let value = Value::new(&[5]).unwrap();
    let write = client
        .submit(Command::Write { attribute, value }, 8, 40)
        .unwrap();
    driver.push(completed(write, Err(Failure::Att(0x03))));
    assert!(matches!(client.poll(9).unwrap(), Update::Finished(outcome)
        if outcome.id == write && outcome.result == Err(Failure::Att(0x03))));
    assert_eq!(client.state(), State::Connected(link));
    assert!(driver.recoveries().is_empty());
}

#[test]
fn os_normalized_remote_rejections_preserve_the_link_without_inventing_att_codes() {
    for error in [
        Error::PermissionDenied,
        Error::Unsupported,
        Error::InvalidValue,
    ] {
        let (mut client, driver) = new_client();
        let link = connect(&mut client, &driver, 0);
        let attribute = attr(driver.clone(), &mut client, 2);
        let read = client.submit(Command::Read { attribute }, 4, 40).unwrap();
        driver.push(completed(read, Err(Failure::Remote(error))));
        assert!(matches!(client.poll(5).unwrap(), Update::Finished(outcome)
            if outcome.id == read && outcome.result == Err(Failure::Remote(error))));
        assert_eq!(client.state(), State::Connected(link));
        assert!(driver.recoveries().is_empty());
        assert!(client.submit(Command::Read { attribute }, 6, 40).is_ok());
    }
}

#[test]
fn local_completion_failure_starts_recovery_and_invalidates_the_link() {
    let (mut client, driver) = new_client();
    let link = connect(&mut client, &driver, 0);
    let attribute = attr(driver.clone(), &mut client, 2);
    let read = client.submit(Command::Read { attribute }, 4, 40).unwrap();
    driver.push(completed(read, Err(Failure::Device(Error::Io))));
    assert!(matches!(client.poll(5).unwrap(), Update::Finished(outcome)
        if outcome.id == read && outcome.result == Err(Failure::Device(Error::Io))));
    assert_eq!(driver.recoveries(), vec![read]);
    assert_eq!(client.state(), State::Recovering);

    let stale_read = Command::Read { attribute };
    let rejected = client.submit(stale_read, 6, 40).unwrap_err();
    assert_eq!(rejected.value, stale_read);
    assert_eq!(rejected.error, ControllerError::Device(Error::Busy));
    driver.push(event(EventKind::Quiescent { recovery: read }));
    assert_eq!(client.poll(7).unwrap(), Update::Recovered);
    assert_eq!(client.state(), State::Idle);
    assert_ne!(client.state(), State::Connected(link));
}

#[test]
fn protocol_completion_mismatch_recovers_and_sets_sticky_fault() {
    let (mut client, driver) = new_client();
    let link = connect(&mut client, &driver, 0);
    let attribute = attr(driver.clone(), &mut client, 2);
    let read = client.submit(Command::Read { attribute }, 4, 40).unwrap();
    driver.push(completed(read, Ok(Reply::Written)));
    assert!(matches!(client.poll(5).unwrap(), Update::Finished(outcome)
        if outcome.id == read && outcome.result == Err(Failure::Protocol)));
    assert_eq!(client.fault(), Some(Failure::Protocol));
    assert_eq!(client.state(), State::Recovering);
    assert_eq!(driver.recoveries(), vec![read]);
    driver.push(event(EventKind::Quiescent { recovery: read }));
    assert_eq!(client.poll(6).unwrap(), Update::Recovered);
    assert_eq!(client.fault(), Some(Failure::Protocol));
    assert_eq!(client.state(), State::Idle);
    assert_ne!(client.state(), State::Connected(link));
}

#[test]
fn cancel_fails_pending_request_and_admission_waits_for_quiescence() {
    let (mut client, driver) = new_client();
    let id = client
        .submit(Command::Connect { peer: peer(1) }, 1, 30)
        .unwrap();
    assert_eq!(
        client.cancel(2).unwrap(),
        Update::Finished(ble::gatt::Outcome {
            id,
            result: Err(Failure::Cancelled),
        })
    );
    assert_eq!(driver.recoveries(), vec![id]);
    let command = Command::Connect { peer: peer(2) };
    let rejected = client.submit(command, 3, 30).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::Device(Error::Busy));
    driver.push(event(EventKind::Quiescent { recovery: id }));
    assert_eq!(client.poll(4).unwrap(), Update::Recovered);
    assert!(client.submit(command, 5, 30).is_ok());
}

#[test]
fn cancel_at_deadline_reports_timeout_instead_of_cancelled() {
    let (mut client, driver) = new_client();
    let id = client
        .submit(Command::Connect { peer: peer(1) }, 3, 4)
        .unwrap();
    assert_eq!(
        client.cancel(7).unwrap(),
        Update::Finished(ble::gatt::Outcome {
            id,
            result: Err(Failure::Device(Error::Timeout)),
        })
    );
    assert_eq!(driver.recoveries(), vec![id]);
    assert_eq!(client.state(), State::Recovering);
}

#[test]
fn cancel_without_pending_work_is_a_noop() {
    let (mut client, driver) = new_client();
    assert_eq!(client.cancel(0).unwrap(), Update::None);
    assert_eq!(client.state(), State::Idle);
    assert!(driver.attempts().is_empty());
    assert!(driver.recoveries().is_empty());
}

#[test]
fn recovery_timeout_is_sticky_even_when_late_barrier_releases_resources() {
    let (mut client, driver) = new_client();
    let id = client
        .submit(Command::Connect { peer: peer(1) }, 0, 2)
        .unwrap();
    assert!(matches!(client.poll(2).unwrap(), Update::Finished(outcome)
        if outcome.id == id && outcome.result == Err(Failure::Device(Error::Timeout))));
    assert_eq!(client.poll(12).unwrap(), Update::RecoveryTimedOut);
    assert_eq!(client.state(), State::Failed);
    assert_eq!(client.fault(), Some(Failure::Device(Error::Timeout)));
    assert_eq!(client.poll(13).unwrap(), Update::None);

    driver.push(event(EventKind::Quiescent { recovery: id }));
    assert_eq!(client.poll(14).unwrap(), Update::Released);
    assert_eq!(client.state(), State::Failed);
    assert_eq!(client.fault(), Some(Failure::Device(Error::Timeout)));
    let command = Command::Connect { peer: peer(2) };
    let rejected = client.submit(command, 15, 20).unwrap_err();
    assert_eq!(rejected.value, command);
    assert_eq!(rejected.error, ControllerError::Device(Error::Unavailable));
}
