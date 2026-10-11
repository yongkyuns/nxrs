use super::*;
use crate::providers::nuttx::{
    normalize_scan, read_scan_with_retry, Field, Lease, SCAN_READ_TIMEOUT,
};
use std::vec;
use std::vec::Vec;

#[test]
fn pending_scan_reads_are_retried_without_hiding_terminal_errors() {
    let mut attempts = 0;
    let result = read_scan_with_retry(
        || {
            attempts += 1;
            if attempts < 3 {
                Err(Error::Busy)
            } else {
                Ok(17)
            }
        },
        SCAN_READ_TIMEOUT,
    );
    assert_eq!(result, Ok(17));
    assert_eq!(attempts, 3);
    assert_eq!(
        read_scan_with_retry(|| Err(Error::Io), SCAN_READ_TIMEOUT),
        Err(Error::Io)
    );
    assert_eq!(
        read_scan_with_retry(|| Err(Error::Busy), Duration::ZERO),
        Err(Error::Timeout)
    );
}
use crate::{Credentials, Passphrase, RequestId};
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::time::Instant;

const WAIT: Duration = Duration::from_secs(2);
struct Sink(SyncSender<Event>);
impl EventSink<Event> for Sink {
    fn try_emit(&mut self, event: Event) -> Result<(), Rejected<Event, DeliveryError>> {
        self.0.try_send(event).map_err(|error| match error {
            TrySendError::Full(value) => Rejected {
                value,
                error: DeliveryError::Full,
            },
            TrySendError::Disconnected(value) => Rejected {
                value,
                error: DeliveryError::Closed,
            },
        })
    }
}

struct Fake {
    fields: Vec<Field>,
    gate: Option<Receiver<()>>,
    entered: Option<SyncSender<()>>,
    error: Option<Error>,
    no_progress: bool,
    station: bool,
    link: LinkState,
    connect_error: Option<Error>,
    close_failures: Arc<AtomicU32>,
    close_calls: Option<SyncSender<()>>,
}
impl Fake {
    fn new(fields: Vec<Field>) -> Self {
        Self {
            fields,
            gate: None,
            entered: None,
            error: None,
            no_progress: false,
            station: false,
            link: LinkState::Unknown,
            connect_error: None,
            close_failures: Arc::new(AtomicU32::new(0)),
            close_calls: None,
        }
    }
    fn wait_native(&self) {
        if let Some(entered) = &self.entered {
            entered.send(()).unwrap();
        }
        if let Some(gate) = &self.gate {
            gate.recv_timeout(WAIT).unwrap();
        }
    }
}
impl Driver for Fake {
    fn capabilities(&self) -> Capabilities {
        Capabilities {
            scan: true,
            open_network: self.station,
            wpa2_personal: self.station,
        }
    }
    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error> {
        self.wait_native();
        assert_eq!(ssid.as_bytes(), b"test");
        match credentials {
            Credentials::Wpa2Personal(value) => {
                assert_eq!(value.as_bytes(), b"test-only-passphrase")
            }
            Credentials::Open => {}
        }
        if let Some(error) = self.connect_error.take() {
            return Err(error);
        }
        self.link = LinkState::Addressed;
        Ok(())
    }
    fn disconnect(&mut self) -> Result<(), Error> {
        self.link = LinkState::Disconnected;
        Ok(())
    }
    fn link(&mut self) -> Result<LinkState, Error> {
        Ok(self.link)
    }
    fn scan(&mut self, emit: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error> {
        self.wait_native();
        if let Some(error) = self.error {
            return Err(error);
        }
        normalize_scan(
            self.fields.len(),
            |offset| {
                let field = self.fields.get(*offset).copied();
                if !self.no_progress {
                    *offset += 1;
                }
                Ok(field)
            },
            emit,
        )
    }
    fn close(&mut self) -> Result<(), Error> {
        if let Some(calls) = &self.close_calls {
            let _ = calls.try_send(());
        }
        if self
            .close_failures
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |value| {
                value.checked_sub(1)
            })
            .is_ok()
        {
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
}

fn field(kind: u32, flags: u32, bytes: &[u8]) -> Field {
    let mut field = Field {
        kind,
        flags,
        length: bytes.len() as u32,
        ..Field::default()
    };
    field.data[..bytes.len()].copy_from_slice(bytes);
    field
}
fn fixture(fake: Fake, capacity: usize) -> (Worker, Receiver<Event>) {
    let (tx, rx) = mpsc::sync_channel(capacity);
    let device = Worker::start(fake, Sink(tx), ()).unwrap();
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Status(ProviderStatus::Ready)
    ));
    (device, rx)
}
fn submit(device: &mut Worker) {
    let id = RequestId {
        session: device.session(),
        operation: OperationId::new(1).unwrap(),
    };
    admit(
        device,
        Request {
            id,
            command: Command::Scan,
        },
    );
}
fn admit(device: &mut Worker, mut request: Request) {
    let id = request.id;
    let deadline = Instant::now() + WAIT;
    loop {
        match device.submit(request) {
            Ok(()) => return,
            Err(rejected) if rejected.error == Error::Busy => {
                // Retry only rejected admission, preserving owned credentials.
                request = Request {
                    id,
                    command: rejected.value,
                };
                assert!(Instant::now() < deadline);
                thread::yield_now();
            }
            Err(rejected) => panic!("unexpected rejection: {rejected:?}"),
        }
    }
}
fn stop(device: &mut Worker) {
    device.request_stop().unwrap();
    let deadline = Instant::now() + WAIT;
    // Only the test waits on a sticky snapshot, not the production owner.
    while device.status() != ProviderStatus::Stopped {
        assert!(Instant::now() < deadline, "provider did not stop");
        thread::yield_now();
    }
    device.finish().unwrap();
}

#[test]
fn normalized_results_precede_completion_and_never_guess_wpa2() {
    let fake = Fake::new(vec![
        field(1, 0, &[]),
        field(2, 0, b"visible"),
        field(3, 3, &[(-42i8) as u8]),
        field(4, 0, &[]),
        field(1, 0, &[]),
        field(2, 0, &[]),
        field(4, 1, &[]),
    ]);
    let (mut device, rx) = fixture(fake, 8);
    submit(&mut device);
    let first = rx.recv_timeout(WAIT).unwrap();
    let second = rx.recv_timeout(WAIT).unwrap();
    assert!(matches!(
        first.kind,
        EventKind::ScanResult {
            access_point: AccessPoint {
                security: Security::Other,
                rssi_dbm: Some(-42),
                ..
            },
            ..
        }
    ));
    assert!(matches!(
        second.kind,
        EventKind::ScanResult {
            access_point: AccessPoint {
                ssid: None,
                security: Security::Open,
                rssi_dbm: None
            },
            ..
        }
    ));
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Completed {
            result: Ok(Completion::Scan {
                reported: 2,
                truncated: true
            }),
            ..
        }
    ));
    stop(&mut device);
    device.request_stop().unwrap();
    device.finish().unwrap();
}

#[test]
fn unsupported_connect_returns_the_owned_credentials_and_wrong_session_is_rejected() {
    let (mut device, _rx) = fixture(Fake::new(vec![]), 4);
    let id = RequestId {
        session: device.session(),
        operation: OperationId::new(1).unwrap(),
    };
    let rejection = device
        .submit(Request {
            id,
            command: Command::Connect {
                ssid: Ssid::new(b"test").unwrap(),
                credentials: Credentials::Wpa2Personal(
                    Passphrase::new("test-only-passphrase").unwrap(),
                ),
            },
        })
        .unwrap_err();
    assert_eq!(rejection.error, Error::Unsupported);
    match rejection.value {
        Command::Connect {
            credentials: Credentials::Wpa2Personal(secret),
            ..
        } => {
            assert_eq!(secret.as_bytes(), b"test-only-passphrase");
        }
        _ => panic!("credentials were lost"),
    }
    let wrong = RequestId {
        session: SessionId::new(u64::MAX).unwrap(),
        ..id
    };
    assert_eq!(
        device
            .submit(Request {
                id: wrong,
                command: Command::Scan
            })
            .unwrap_err()
            .error,
        Error::InvalidConfig
    );
    assert!(!device.capabilities().wpa2_personal);
    stop(&mut device);
}

#[test]
fn stop_during_native_wait_is_nonblocking_and_waits_for_quiescence_before_join() {
    let (gate_tx, gate_rx) = mpsc::sync_channel(1);
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let mut fake = Fake::new(vec![field(1, 0, &[])]);
    fake.gate = Some(gate_rx);
    fake.entered = Some(entered_tx);
    let (mut device, rx) = fixture(fake, 4);
    submit(&mut device);
    entered_rx.recv_timeout(WAIT).unwrap();
    assert_eq!(device.finish(), Err(Error::Busy));
    assert_eq!(
        device
            .submit(Request {
                id: RequestId {
                    session: device.session(),
                    operation: OperationId::new(2).unwrap()
                },
                command: Command::Scan,
            })
            .unwrap_err()
            .error,
        Error::Busy
    );
    device.request_stop().unwrap();
    assert_eq!(device.status(), ProviderStatus::Stopping);
    assert_eq!(device.finish(), Err(Error::Busy));
    gate_tx.send(()).unwrap();
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Status(ProviderStatus::Stopped)
    ));
    device.finish().unwrap();
    assert!(rx.try_recv().is_err(), "a result escaped after stop");
}

#[test]
fn a_full_event_queue_cannot_block_stop_or_overrun_the_buffer() {
    let (mut device, rx) = fixture(Fake::new(vec![field(1, 0, &[]); 4]), 1);
    submit(&mut device);
    // Peek by consuming the first result, then let the second saturate ingress.
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::ScanResult { .. }
    ));
    stop(&mut device);
    assert!(rx
        .try_iter()
        .all(|event| !matches!(event.kind, EventKind::Completed { .. })));
}

#[test]
fn delivery_closure_quiesces_the_worker() {
    let (mut device, rx) = fixture(Fake::new(vec![field(1, 0, &[])]), 1);
    drop(rx);
    submit(&mut device);
    let deadline = Instant::now() + WAIT;
    while device.status() != ProviderStatus::Stopped {
        assert!(Instant::now() < deadline);
        thread::yield_now();
    }
    device.finish().unwrap();
}

#[test]
fn malformed_normalization_and_native_failures_require_explicit_stop() {
    let mut oversized = field(2, 0, b"x");
    oversized.length = 33;
    let mut stalled = Fake::new(vec![field(1, 0, &[])]);
    stalled.no_progress = true;
    let mut native_error = Fake::new(vec![]);
    native_error.error = Some(Error::Io);
    for fake in [
        Fake::new(vec![field(2, 0, b"orphan")]),
        Fake::new(vec![field(1, 0, &[]), oversized]),
        stalled,
        native_error,
    ] {
        let (mut device, rx) = fixture(fake, 4);
        submit(&mut device);
        assert!(matches!(
            rx.recv_timeout(WAIT).unwrap().kind,
            EventKind::Status(ProviderStatus::Failed(_))
        ));
        assert_eq!(device.finish(), Err(Error::Busy));
        assert!(device.shared.pending.lock().unwrap().busy);
        stop(&mut device);
    }
}

#[test]
fn restart_has_a_new_session_and_idle_stop_has_no_lost_wakeup() {
    let mut previous = None;
    for _ in 0..32 {
        let (mut device, _rx) = fixture(Fake::new(vec![]), 1);
        assert_ne!(previous, Some(device.session()));
        previous = Some(device.session());
        stop(&mut device);
    }
}

#[test]
fn status_roundtrip_covers_every_native_error() {
    for error in [
        Error::Unsupported,
        Error::Busy,
        Error::Unavailable,
        Error::PermissionDenied,
        Error::AuthenticationFailed,
        Error::InvalidConfig,
        Error::Timeout,
        Error::Protocol,
        Error::Io,
    ] {
        let status = ProviderStatus::Failed(error);
        assert_eq!(decode_status(encode_status(status)), status);
    }
}

#[test]
fn interface_lease_is_exclusive_and_released_by_raii() {
    let lease = Lease::acquire().unwrap();
    assert!(matches!(Lease::acquire(), Err(Error::Busy)));
    drop(lease);
    drop(Lease::acquire().unwrap());
}

fn connect_request(device: &Worker, operation: u64) -> Request {
    Request {
        id: RequestId {
            session: device.session(),
            operation: OperationId::new(operation).unwrap(),
        },
        command: Command::Connect {
            ssid: Ssid::new(b"test").unwrap(),
            credentials: Credentials::Wpa2Personal(
                Passphrase::new("test-only-passphrase").unwrap(),
            ),
        },
    }
}

#[test]
fn station_delivers_addressed_before_connected_and_disconnects_explicitly() {
    let mut fake = Fake::new(vec![]);
    fake.station = true;
    let (mut device, rx) = fixture(fake, 4);
    assert!(device.capabilities().wpa2_personal);
    let request = connect_request(&device, 1);
    admit(&mut device, request);
    assert_eq!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::LinkChanged(LinkState::Addressed)
    );
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Completed {
            result: Ok(Completion::Connected),
            ..
        }
    ));
    let request = Request {
        id: RequestId {
            session: device.session(),
            operation: OperationId::new(2).unwrap(),
        },
        command: Command::Disconnect,
    };
    admit(&mut device, request);
    assert_eq!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::LinkChanged(LinkState::Disconnected)
    );
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Completed {
            result: Ok(Completion::Disconnected),
            ..
        }
    ));
    stop(&mut device);
}

#[test]
fn a_clean_connect_timeout_allows_retry_but_unknown_native_failure_faults() {
    for error in [Error::Timeout, Error::Io] {
        let mut fake = Fake::new(vec![]);
        fake.station = true;
        fake.connect_error = Some(error);
        let (mut device, rx) = fixture(fake, 4);
        let request = connect_request(&device, 1);
        admit(&mut device, request);
        if error == Error::Timeout {
            assert!(matches!(
                rx.recv_timeout(WAIT).unwrap().kind,
                EventKind::Completed {
                    result: Err(Error::Timeout),
                    ..
                }
            ));
            assert_eq!(device.status(), ProviderStatus::Ready);
            let request = connect_request(&device, 2);
            admit(&mut device, request);
            assert_eq!(
                rx.recv_timeout(WAIT).unwrap().kind,
                EventKind::LinkChanged(LinkState::Addressed)
            );
            assert!(matches!(
                rx.recv_timeout(WAIT).unwrap().kind,
                EventKind::Completed {
                    result: Ok(Completion::Connected),
                    ..
                }
            ));
        } else {
            assert_eq!(
                rx.recv_timeout(WAIT).unwrap().kind,
                EventKind::Status(ProviderStatus::Failed(Error::Io))
            );
            assert_eq!(
                device
                    .submit(connect_request(&device, 2))
                    .unwrap_err()
                    .error,
                Error::Unavailable
            );
        }
        stop(&mut device);
    }
}

#[test]
fn stopping_during_connect_suppresses_link_and_completion_until_quiescent() {
    let (gate_tx, gate_rx) = mpsc::sync_channel(1);
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let mut fake = Fake::new(vec![]);
    fake.station = true;
    fake.gate = Some(gate_rx);
    fake.entered = Some(entered_tx);
    let (mut device, rx) = fixture(fake, 4);
    let request = connect_request(&device, 1);
    admit(&mut device, request);
    entered_rx.recv_timeout(WAIT).unwrap();
    device.request_stop().unwrap();
    assert_eq!(device.finish(), Err(Error::Busy));
    gate_tx.send(()).unwrap();
    assert_eq!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Status(ProviderStatus::Stopped)
    );
    device.finish().unwrap();
    assert!(rx.try_recv().is_err());
}

#[test]
fn fault_survives_full_delivery_and_stop_before_owner_dispatch() {
    let mut malformed = field(2, 0, b"bad");
    malformed.length = 33;
    let fake = Fake::new(vec![field(1, 0, &[]), field(1, 0, &[]), malformed]);
    let (mut device, rx) = fixture(fake, 1);
    submit(&mut device);
    let deadline = Instant::now() + WAIT;
    while !matches!(device.status(), ProviderStatus::Failed(_)) {
        assert!(Instant::now() < deadline);
        thread::yield_now();
    }
    // The single queue slot holds a result, not the fault event.
    assert_eq!(device.fault(), Some(Error::Protocol));
    stop(&mut device);
    assert_eq!(device.fault(), Some(Error::Protocol));
    assert!(rx
        .try_iter()
        .all(|event| !matches!(event.kind, EventKind::Status(ProviderStatus::Failed(_)))));
}

#[test]
fn failed_cleanup_retains_driver_until_explicit_retry() {
    let fake = Fake::new(vec![]);
    fake.close_failures.store(1, Ordering::Release);
    let (mut device, rx) = fixture(fake, 4);
    device.request_stop().unwrap();
    assert_eq!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Status(ProviderStatus::Failed(Error::Io))
    );
    assert_eq!(device.status(), ProviderStatus::Failed(Error::Io));
    assert_eq!(device.finish(), Err(Error::Busy));
    assert_eq!(device.fault(), Some(Error::Io));
    device.request_stop().unwrap();
    assert_eq!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Status(ProviderStatus::Stopped)
    );
    device.finish().unwrap();
    assert_eq!(device.fault(), Some(Error::Io));
}

#[test]
fn drop_retries_abandoned_cleanup_without_releasing_guard_early() {
    struct Guard(SyncSender<()>);
    impl Drop for Guard {
        fn drop(&mut self) {
            self.0.try_send(()).unwrap();
        }
    }
    let (released_tx, released_rx) = mpsc::sync_channel(1);
    let (calls_tx, calls_rx) = mpsc::sync_channel(8);
    let (tx, rx) = mpsc::sync_channel(4);
    let mut fake = Fake::new(vec![]);
    fake.close_failures.store(2, Ordering::Release);
    fake.close_calls = Some(calls_tx);
    let mut device = Worker::start(fake, Sink(tx), Guard(released_tx)).unwrap();
    assert_eq!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Status(ProviderStatus::Ready)
    );
    device.request_stop().unwrap();
    calls_rx.recv_timeout(WAIT).unwrap();
    assert_eq!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Status(ProviderStatus::Failed(Error::Io))
    );
    assert!(released_rx.try_recv().is_err());
    let dropped_at = Instant::now();
    drop(device);
    calls_rx.recv_timeout(WAIT).unwrap();
    calls_rx.recv_timeout(WAIT).unwrap();
    released_rx.recv_timeout(WAIT).unwrap();
    assert!(dropped_at.elapsed() >= CLEANUP_RETRY);
}
