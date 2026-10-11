use super::*;
use crate::worker::{ScanReport, Scanner, Worker};
use crate::*;
use crate::{OperationId, RequestId};
use std::collections::VecDeque;
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::{Duration, Instant};
use std::vec;
use std::vec::Vec;

const WAIT: Duration = Duration::from_secs(3);
static TEST_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
fn serialized() -> MutexGuard<'static, ()> {
    TEST_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

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

#[derive(Default)]
struct FakeState {
    starts: usize,
    gets: usize,
    stops: usize,
    closes: usize,
    reports: VecDeque<ScanReport<u8>>,
    stop_failures: usize,
    persistent_stop_failure: bool,
    check_entered: Option<SyncSender<()>>,
    check_gate: Option<Receiver<()>>,
}
struct Fake(Arc<Mutex<FakeState>>);
impl Scanner for Fake {
    type Peer = u8;
    fn check(&mut self) -> Result<(), Error> {
        let (entered, gate) = {
            let mut state = self.0.lock().unwrap();
            (state.check_entered.take(), state.check_gate.take())
        };
        if let Some(entered) = entered {
            entered.send(()).unwrap();
        }
        if let Some(gate) = gate {
            gate.recv_timeout(WAIT).unwrap();
        }
        Ok(())
    }
    fn start_scan(&mut self) -> Result<(), Error> {
        self.0.lock().unwrap().starts += 1;
        Ok(())
    }
    fn get(
        &mut self,
        _service: Option<Uuid>,
        output: &mut [ScanReport<u8>],
    ) -> Result<usize, Error> {
        let mut state = self.0.lock().unwrap();
        state.gets += 1;
        let count = output.len().min(state.reports.len());
        for destination in output.iter_mut().take(count) {
            *destination = state.reports.pop_front().unwrap();
        }
        Ok(count)
    }
    fn stop_scan(&mut self) -> Result<(), Error> {
        let mut state = self.0.lock().unwrap();
        state.stops += 1;
        if state.persistent_stop_failure || state.stop_failures > 0 {
            state.stop_failures = state.stop_failures.saturating_sub(1);
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
    fn close(&mut self) {
        self.0.lock().unwrap().closes += 1;
    }
}

fn report(address: u8) -> ScanReport<u8> {
    ScanReport {
        peer: address,
        rssi_dbm: Some(-47),
        matches_filter: true,
    }
}
fn fixture(capacity: usize) -> (Worker, Receiver<Event>, Arc<Mutex<FakeState>>) {
    let state = Arc::new(Mutex::new(FakeState::default()));
    let (tx, rx) = mpsc::sync_channel(capacity);
    let device = Worker::start(
        Fake(Arc::clone(&state)),
        Sink(tx),
        Lease::acquire().unwrap(),
    )
    .unwrap();
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Status(ProviderStatus::Ready)
    ));
    (device, rx, state)
}
fn request(device: &Worker, operation: u64, command: Command) -> Request {
    Request {
        id: RequestId {
            session: device.session(),
            operation: OperationId::new(operation).unwrap(),
        },
        command,
    }
}
fn wait_for(mut condition: impl FnMut() -> bool, message: &str) {
    let deadline = Instant::now() + WAIT;
    while !condition() {
        assert!(Instant::now() < deadline, "{message}");
        thread::sleep(Duration::from_millis(1));
    }
}
fn start(device: &mut Worker, rx: &Receiver<Event>) {
    admit(device, 1, Command::StartScan { service: None });
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Completed {
            result: Ok(Completion::ScanStarted),
            ..
        }
    ));
}
fn admit(device: &mut Worker, operation: u64, command: Command) {
    let mut request = request(device, operation, command);
    let deadline = Instant::now() + WAIT;
    loop {
        match device.submit(request) {
            Ok(()) => return,
            Err(rejected) if rejected.error == Error::Busy => {
                // The worker may briefly hold the nonblocking admission slot.
                request = rejected.value;
                assert!(Instant::now() < deadline);
                thread::yield_now();
            }
            Err(rejected) => panic!("unexpected rejection: {rejected:?}"),
        }
    }
}
fn finish(device: &mut Worker) {
    device.request_stop().unwrap();
    wait_for(
        || device.status() == ProviderStatus::Stopped,
        "provider did not stop",
    );
    device.finish().unwrap();
}

#[test]
fn compact_scan_api_admits_only_scanning_and_keeps_normal_stop() {
    let _guard = serialized();
    let (mut device, rx, _) = fixture(8);
    assert!(device.capabilities().scan);
    admit(
        &mut device,
        1,
        Command::StartScan {
            service: Some(Uuid([7; 16])),
        },
    );
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Completed {
            result: Ok(Completion::ScanStarted),
            ..
        }
    ));
    admit(&mut device, 2, Command::StopScan);
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Completed {
            result: Ok(Completion::ScanStopped),
            ..
        }
    ));
    finish(&mut device);
}

#[test]
fn discovery_table_saturates_once_per_scan_without_recycling_peer_ids() {
    let _guard = serialized();
    let (mut device, rx, state) = fixture(128);
    state.lock().unwrap().reports.extend((1..=34).map(report));
    start(&mut device, &rx);
    let mut first_ids = Vec::new();
    let mut limited = 0;
    wait_for(
        || {
            for event in rx.try_iter() {
                match event.kind {
                    EventKind::Discovered { peer, .. } => first_ids.push(peer),
                    EventKind::DiscoveryLimited { limit: 32 } => limited += 1,
                    _ => {}
                }
            }
            limited == 1
        },
        "first scan did not report discovery truncation",
    );
    assert_eq!(first_ids.len(), 32);
    let unique: std::collections::HashSet<_> = first_ids.iter().map(|id| id.0).collect();
    assert_eq!(unique.len(), 32);
    admit(&mut device, 2, Command::StopScan);
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Completed {
            result: Ok(Completion::ScanStopped),
            ..
        }
    ));

    state.lock().unwrap().reports.extend((1..=34).map(report));
    admit(&mut device, 3, Command::StartScan { service: None });
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Completed {
            result: Ok(Completion::ScanStarted),
            ..
        }
    ));
    let mut second_ids = Vec::new();
    let mut second_limited = 0;
    wait_for(
        || {
            for event in rx.try_iter() {
                match event.kind {
                    EventKind::Discovered { peer, .. } => second_ids.push(peer),
                    EventKind::DiscoveryLimited { limit: 32 } => second_limited += 1,
                    _ => {}
                }
            }
            second_limited == 1
        },
        "second scan did not report truncation",
    );
    assert_eq!(second_ids.len(), 32);
    assert_eq!(first_ids, second_ids);
    admit(&mut device, 4, Command::StopScan);
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Completed {
            result: Ok(Completion::ScanStopped),
            ..
        }
    ));
    finish(&mut device);
}

#[test]
fn owner_can_retry_failed_stop_and_fault_stays_sticky() {
    let _guard = serialized();
    let (mut device, rx, state) = fixture(16);
    start(&mut device, &rx);
    state.lock().unwrap().stop_failures = 1;
    device.request_stop().unwrap();
    wait_for(
        || device.status() == ProviderStatus::Failed(Error::Io),
        "stop fault was not published",
    );
    assert_eq!(device.finish(), Err(Error::Busy));
    assert_eq!(Lease::acquire().unwrap_err(), Error::Busy);
    device.request_stop().unwrap();
    wait_for(
        || device.status() == ProviderStatus::Stopped,
        "owner retry did not quiet scanner",
    );
    device.finish().unwrap();
    assert_eq!(device.fault(), Some(Error::Io));
}

#[test]
fn dropping_owner_retries_transient_stop_failure_and_releases_lease() {
    let _guard = serialized();
    let (mut device, rx, state) = fixture(16);
    start(&mut device, &rx);
    state.lock().unwrap().stop_failures = 1;
    drop(device);
    wait_for(
        || state.lock().unwrap().stops >= 2,
        "drop did not trigger bounded stop retry",
    );
    wait_for(
        || state.lock().unwrap().closes == 1,
        "scanner was not closed after retry succeeded",
    );
    let lease = Lease::acquire().expect("lease should release only after native stop and close");
    drop(lease);
}

#[test]
fn persistent_drop_failure_retains_lease_and_retries_with_backoff() {
    let _guard = serialized();
    let (mut device, rx, state) = fixture(16);
    start(&mut device, &rx);
    state.lock().unwrap().persistent_stop_failure = true;
    drop(device);
    thread::sleep(Duration::from_millis(400));
    let attempts = state.lock().unwrap().stops;
    assert!(attempts >= 2, "abandoned worker did not retry stop");
    assert!(attempts <= 3, "stop retries were not backoff bounded");
    assert_eq!(Lease::acquire().unwrap_err(), Error::Busy);
    assert_eq!(state.lock().unwrap().closes, 0);
    // Restore the fake radio so the test does not leave a worker or global lease behind.
    state.lock().unwrap().persistent_stop_failure = false;
    wait_for(
        || state.lock().unwrap().closes == 1,
        "worker did not release after native stop recovered",
    );
    drop(Lease::acquire().unwrap());
}

#[test]
fn native_service_filter_normalizes_16_32_and_128_bit_advertisements() {
    let uuid16 = Uuid([
        0, 0, 0x18, 0x0d, 0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb,
    ]);
    assert!(super::native::advertisement_has_service(
        &[3, 0x03, 0x0d, 0x18],
        uuid16
    ));
    let uuid32 = Uuid([
        0x78, 0x56, 0x34, 0x12, 0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb,
    ]);
    assert!(super::native::advertisement_has_service(
        &[5, 0x05, 0x12, 0x34, 0x56, 0x78],
        uuid32
    ));
    let uuid128 = Uuid([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]);
    let mut ad = vec![17, 0x07];
    ad.extend((0..16).rev());
    assert!(super::native::advertisement_has_service(&ad, uuid128));
    assert!(!super::native::advertisement_has_service(&[0], uuid128));
}

#[test]
fn stop_before_native_readiness_does_not_publish_ready_or_lose_stop() {
    let _guard = serialized();
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (gate_tx, gate_rx) = mpsc::sync_channel(1);
    let state = Arc::new(Mutex::new(FakeState {
        check_entered: Some(entered_tx),
        check_gate: Some(gate_rx),
        ..FakeState::default()
    }));
    let (tx, rx) = mpsc::sync_channel(1);
    let mut device =
        Worker::start(Fake(state.clone()), Sink(tx), Lease::acquire().unwrap()).unwrap();
    entered_rx.recv_timeout(WAIT).unwrap();
    device.request_stop().unwrap();
    assert_eq!(device.finish(), Err(Error::Busy));
    assert_eq!(Lease::acquire().unwrap_err(), Error::Busy);
    gate_tx.send(()).unwrap();
    assert_eq!(
        rx.recv_timeout(WAIT).unwrap().kind,
        EventKind::Status(ProviderStatus::Stopped)
    );
    device.finish().unwrap();
    assert_eq!(device.fault(), None);
    assert!(rx.try_recv().is_err());
}

#[test]
fn full_discovery_sink_does_not_block_explicit_stop_or_duplicate_start() {
    let _guard = serialized();
    let (mut device, rx, state) = fixture(1);
    start(&mut device, &rx);
    let duplicate = request(&device, 2, Command::StartScan { service: None });
    assert_eq!(device.submit(duplicate).unwrap_err().error, Error::Busy);
    state.lock().unwrap().reports.extend((1..=8).map(report));
    wait_for(
        || state.lock().unwrap().reports.is_empty(),
        "reports were not fetched",
    );
    // Discovery cannot drain while rx is not consumed, but stop has independent capacity.
    device.request_stop().unwrap();
    wait_for(
        || device.status() == ProviderStatus::Stopped,
        "full delivery blocked stop",
    );
    device.finish().unwrap();
    assert_eq!(device.fault(), None);
    assert_eq!(state.lock().unwrap().closes, 1);
}

#[test]
fn closed_discovery_sink_faults_but_owner_can_reclaim_native_resources() {
    let _guard = serialized();
    let (mut device, rx, state) = fixture(1);
    start(&mut device, &rx);
    drop(rx);
    state.lock().unwrap().reports.push_back(report(1));
    wait_for(
        || device.fault() == Some(Error::Io),
        "closed sink did not retain a fault",
    );
    finish(&mut device);
    assert_eq!(device.fault(), Some(Error::Io));
    assert_eq!(state.lock().unwrap().closes, 1);
}
