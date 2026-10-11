use super::*;
use crate::{OperationId, RequestId};
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::sync::Barrier;
use std::sync::{MutexGuard, OnceLock};
use std::time::Instant;
use std::vec;
use std::vec::Vec;

const WAIT: Duration = Duration::from_secs(2);
static TEST_LOCK: OnceLock<std::sync::Mutex<()>> = OnceLock::new();

fn serialized() -> MutexGuard<'static, ()> {
    TEST_LOCK
        .get_or_init(|| std::sync::Mutex::new(()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

struct Sink(SyncSender<ProviderEvent<64>>);
impl EventSink<ProviderEvent<64>> for Sink {
    fn try_emit(
        &mut self,
        event: ProviderEvent<64>,
    ) -> Result<(), Rejected<ProviderEvent<64>, DeliveryError>> {
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
    records: Vec<Record>,
    started_get: Option<SyncSender<()>>,
    get_gate: Option<Receiver<()>>,
    check_entered: Option<SyncSender<()>>,
    check_gate: Option<Receiver<()>>,
    panic_check: bool,
    stop_error: Option<Error>,
}

struct Fake(Arc<Mutex<FakeState>>);
impl NativeScanner for Fake {
    fn check(&mut self) -> Result<(), Error> {
        let (entered, gate, panic_check) = {
            let mut state = self.0.lock().unwrap();
            (
                state.check_entered.take(),
                state.check_gate.take(),
                state.panic_check,
            )
        };
        if panic_check {
            panic!("scripted worker panic");
        }
        if let Some(entered) = entered {
            let _ = entered.send(());
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
    fn get(&mut self, output: &mut [Record]) -> Result<usize, Error> {
        let (entered, gate) = {
            let mut state = self.0.lock().unwrap();
            state.gets += 1;
            (state.started_get.take(), state.get_gate.take())
        };
        if let Some(entered) = entered {
            let _ = entered.send(());
        }
        if let Some(gate) = gate {
            gate.recv_timeout(WAIT).unwrap();
        }
        let mut state = self.0.lock().unwrap();
        let count = state.records.len().min(output.len());
        output[..count].copy_from_slice(&state.records[..count]);
        state.records.drain(..count);
        Ok(count)
    }
    fn stop_scan(&mut self) -> Result<(), Error> {
        let mut state = self.0.lock().unwrap();
        state.stops += 1;
        if let Some(error) = state.stop_error.take() {
            Err(error)
        } else {
            Ok(())
        }
    }
    fn close(&mut self) {
        self.0.lock().unwrap().closes += 1;
    }
}

fn record(address: u8, data: &[u8]) -> Record {
    let mut result = Record {
        address: [address; 6],
        address_type: 1,
        rssi: -47,
        length: data.len() as u8,
        ..Record::default()
    };
    result.data[..data.len()].copy_from_slice(data);
    result
}

fn fixture(
    fake: Fake,
    state: Arc<Mutex<FakeState>>,
    capacity: usize,
) -> (
    NuttxBle<64>,
    Receiver<ProviderEvent<64>>,
    SyncSender<ProviderEvent<64>>,
) {
    let (tx, rx) = mpsc::sync_channel(capacity);
    let device = NuttxBle::start(fake, Sink(tx.clone()), Some(Lease::acquire().unwrap())).unwrap();
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        ProviderEventKind::<64>::Status(ProviderStatus::Ready)
    ));
    drop(state);
    (device, rx, tx)
}

fn request(device: &NuttxBle<64>, operation: u64, command: Command<64>) -> Request<64> {
    Request {
        id: RequestId {
            session: device.session(),
            operation: OperationId::new(operation).unwrap(),
        },
        command,
    }
}

fn stop(device: &mut NuttxBle<64>) {
    device.request_stop().unwrap();
    let deadline = Instant::now() + WAIT;
    while device.status() != ProviderStatus::Stopped {
        assert!(Instant::now() < deadline, "provider did not stop");
        thread::yield_now();
    }
    device.finish().unwrap();
}

#[test]
fn scan_is_advertised_without_claiming_central_qualification_and_rejects_other_commands() {
    let _guard = serialized();
    let state = Arc::new(Mutex::new(FakeState::default()));
    let (mut device, _rx, _) = fixture(Fake(state.clone()), state, 4);
    assert!(!device.capabilities().central);
    assert!(device.capabilities().scan);

    let unsupported = device
        .submit(request(&device, 1, Command::Connect(PeerId(8))))
        .unwrap_err();
    assert_eq!(unsupported.error, Error::Unsupported);
    assert!(matches!(
        unsupported.value.command,
        Command::Connect(PeerId(8))
    ));

    let mut wrong_session = request(&device, 2, Command::StartScan { service: None });
    wrong_session.id.session = SessionId::new(u64::MAX).unwrap();
    assert_eq!(
        device.submit(wrong_session).unwrap_err().error,
        Error::InvalidValue
    );
    stop(&mut device);
}

#[test]
fn ordinary_slot_rejects_duplicate_start_as_busy() {
    let _guard = serialized();
    let state = Arc::new(Mutex::new(FakeState::default()));
    let (mut device, rx, _) = fixture(Fake(state.clone()), state, 4);
    device
        .submit(request(&device, 1, Command::StartScan { service: None }))
        .unwrap();
    assert_eq!(
        device
            .submit(request(&device, 2, Command::StartScan { service: None }))
            .unwrap_err()
            .error,
        Error::Busy
    );
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        ProviderEventKind::<64>::Completed {
            result: Ok(Completion::ScanStarted),
            ..
        }
    ));
    device
        .submit(request(&device, 3, Command::StopScan))
        .unwrap();
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        ProviderEventKind::<64>::Completed {
            result: Ok(Completion::ScanStopped),
            ..
        }
    ));
    stop(&mut device);
}

#[test]
fn stop_command_interrupts_full_sink_and_completes_only_after_native_stop() {
    let _guard = serialized();
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (gate_tx, gate_rx) = mpsc::sync_channel(1);
    let state = Arc::new(Mutex::new(FakeState {
        records: vec![record(4, &[])],
        started_get: Some(entered_tx),
        get_gate: Some(gate_rx),
        ..FakeState::default()
    }));
    let (mut device, rx, tx) = fixture(Fake(state.clone()), state.clone(), 1);
    device
        .submit(request(&device, 1, Command::StartScan { service: None }))
        .unwrap();
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        ProviderEventKind::<64>::Completed {
            result: Ok(Completion::ScanStarted),
            ..
        }
    ));
    entered_rx.recv_timeout(WAIT).unwrap();
    tx.try_send(ProviderEvent::<64> {
        session: device.session(),
        kind: ProviderEventKind::<64>::Status(ProviderStatus::Ready),
    })
    .unwrap();
    gate_tx.send(()).unwrap();

    device
        .submit(request(&device, 2, Command::StopScan))
        .unwrap();
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        ProviderEventKind::<64>::Status(ProviderStatus::Ready)
    ));
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        ProviderEventKind::<64>::Completed {
            result: Ok(Completion::ScanStopped),
            ..
        }
    ));
    assert_eq!(state.lock().unwrap().stops, 1);
    stop(&mut device);
}

#[test]
fn controller_lease_is_held_until_worker_is_stopped_and_joined() {
    let _guard = serialized();
    let state = Arc::new(Mutex::new(FakeState::default()));
    let (mut device, _rx, _) = fixture(Fake(state.clone()), state, 2);
    assert_eq!(Lease::acquire().unwrap_err(), Error::Busy);
    stop(&mut device);
    device.request_stop().unwrap();
    assert_eq!(device.status(), ProviderStatus::Stopped);
    let lease = Lease::acquire().unwrap();
    drop(lease);
}

#[test]
fn stop_requested_during_gated_startup_is_not_lost() {
    let _guard = serialized();
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (gate_tx, gate_rx) = mpsc::sync_channel(1);
    let state = Arc::new(Mutex::new(FakeState {
        check_entered: Some(entered_tx),
        check_gate: Some(gate_rx),
        ..FakeState::default()
    }));
    let (tx, rx) = mpsc::sync_channel(4);
    let mut device = NuttxBle::<64>::start(
        Fake(state.clone()),
        Sink(tx),
        Some(Lease::acquire().unwrap()),
    )
    .unwrap();

    entered_rx.recv_timeout(WAIT).unwrap();
    device.request_stop().unwrap();
    gate_tx.send(()).unwrap();
    let deadline = Instant::now() + WAIT;
    while device.status() != ProviderStatus::Stopped {
        assert!(Instant::now() < deadline, "pre-start stop was lost");
        thread::yield_now();
    }
    assert!(!rx
        .try_iter()
        .any(|event| { event.kind == ProviderEventKind::<64>::Status(ProviderStatus::Ready) }));
    device.finish().unwrap();
    assert_eq!(state.lock().unwrap().closes, 1);
}

#[test]
fn concurrent_stop_publication_never_overwrites_stopped() {
    let shared = Arc::new(Shared::<64>::new());
    shared.set_status(ProviderStatus::Ready);
    let barrier = Arc::new(Barrier::new(2));
    let stopping_shared = Arc::clone(&shared);
    let stopping_barrier = Arc::clone(&barrier);
    let stopper = thread::spawn(move || {
        stopping_barrier.wait();
        stopping_shared.mark_stopping_unless_terminal();
    });
    barrier.wait();
    shared.set_status(ProviderStatus::Stopped);
    stopper.join().unwrap();
    assert_eq!(shared.status(), ProviderStatus::Stopped);
}

#[test]
fn finish_join_failure_remains_sticky_and_keeps_the_lease() {
    let _guard = serialized();
    let state = Arc::new(Mutex::new(FakeState {
        panic_check: true,
        ..FakeState::default()
    }));
    let (tx, _rx) = mpsc::sync_channel(1);
    let mut device =
        NuttxBle::<64>::start(Fake(state), Sink(tx), Some(Lease::acquire().unwrap())).unwrap();
    let deadline = Instant::now() + WAIT;
    while !device.worker.as_ref().unwrap().is_finished() {
        assert!(Instant::now() < deadline, "scripted worker did not panic");
        thread::yield_now();
    }
    // Make the join path reachable after the test worker's deliberate panic.
    device.shared.set_status(ProviderStatus::Stopped);
    assert_eq!(device.finish(), Err(Error::Io));
    assert_eq!(device.finish(), Err(Error::Io));
    assert_eq!(Lease::acquire().unwrap_err(), Error::Busy);
}

#[test]
fn failed_native_stop_keeps_worker_and_lease_until_a_later_stop_succeeds() {
    let _guard = serialized();
    let state = Arc::new(Mutex::new(FakeState {
        stop_error: Some(Error::Io),
        ..FakeState::default()
    }));
    let (mut device, rx, _) = fixture(Fake(state.clone()), state, 4);
    device
        .submit(request(&device, 1, Command::StartScan { service: None }))
        .unwrap();
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap().kind,
        ProviderEventKind::<64>::Completed {
            result: Ok(Completion::ScanStarted),
            ..
        }
    ));

    device.request_stop().unwrap();
    let deadline = Instant::now() + WAIT;
    while device.status() != ProviderStatus::Failed(Error::Io) {
        assert!(Instant::now() < deadline, "stop failure was not published");
        thread::yield_now();
    }
    assert_eq!(device.finish(), Err(Error::Busy));
    assert_eq!(Lease::acquire().unwrap_err(), Error::Busy);

    device.request_stop().unwrap();
    let deadline = Instant::now() + WAIT;
    while device.status() != ProviderStatus::Stopped {
        assert!(Instant::now() < deadline, "retry did not stop the radio");
        thread::yield_now();
    }
    device.finish().unwrap();
    assert_eq!(device.fault(), Some(Error::Io));
    drop(Lease::acquire().unwrap());
}

#[test]
fn readiness_cannot_resurrect_stopping_or_stopped() {
    let shared = Shared::<64>::new();
    shared.mark_stopping_unless_terminal();
    assert!(!shared.mark_ready());
    assert_eq!(shared.status(), ProviderStatus::Stopping);
    shared.set_status(ProviderStatus::Stopped);
    assert!(!shared.mark_ready());
    assert_eq!(shared.status(), ProviderStatus::Stopped);
}

#[test]
fn ready_delivery_cancelled_by_stop_does_not_create_a_fault() {
    let _guard = serialized();
    let state = Arc::new(Mutex::new(FakeState::default()));
    let (tx, rx) = mpsc::sync_channel(1);
    tx.try_send(ProviderEvent::<64> {
        session: SessionId::new(1).unwrap(),
        kind: ProviderEventKind::Status(ProviderStatus::Starting),
    })
    .unwrap();
    let mut device =
        NuttxBle::<64>::start(Fake(state), Sink(tx), Some(Lease::acquire().unwrap())).unwrap();
    let deadline = Instant::now() + WAIT;
    while device.status() != ProviderStatus::Ready {
        assert!(Instant::now() < deadline);
        thread::yield_now();
    }
    stop(&mut device);
    assert_eq!(device.fault(), None);
    drop(rx);
}

#[test]
fn stop_generation_exhaustion_is_reported_without_wrapping_or_stopping() {
    let _guard = serialized();
    let state = Arc::new(Mutex::new(FakeState::default()));
    let (mut device, _rx, _) = fixture(Fake(state.clone()), state, 2);
    device
        .shared
        .stop_generation
        .store(u32::MAX, Ordering::Release);
    assert_eq!(device.request_stop(), Err(Error::Unavailable));
    assert_eq!(device.shared.stop_generation(), u32::MAX);
    assert!(!device.shared.stop_requested());
    assert_eq!(device.status(), ProviderStatus::Ready);

    // Restore one available epoch so teardown remains orderly in the fixture.
    device
        .shared
        .stop_generation
        .store(u32::MAX - 1, Ordering::Release);
    stop(&mut device);
}

#[test]
fn service_filter_parses_little_endian_16_32_and_128_bit_ad_lists() {
    let uuid16 = Uuid([
        0, 0, 0x18, 0x0d, 0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb,
    ]);
    assert!(advertisement_has_service(&[3, 0x03, 0x0d, 0x18], uuid16));

    let uuid32 = Uuid([
        0x78, 0x56, 0x34, 0x12, 0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb,
    ]);
    assert!(advertisement_has_service(
        &[5, 0x05, 0x12, 0x34, 0x56, 0x78],
        uuid32
    ));

    let uuid128 = Uuid([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]);
    let mut ad = vec![17, 0x07];
    ad.extend((0..16).rev());
    assert!(advertisement_has_service(&ad, uuid128));
}
