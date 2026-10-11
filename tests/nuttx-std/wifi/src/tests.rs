//! Host qualification of the scan fixture's single-wait owner.

use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};
use std::sync::{mpsc, Arc, Barrier};
use std::thread;
use std::time::Duration;

use crate::{channels, CommandSender, OwnerError, OwnerReport, StopAdmissionError, WifiEventSink};
use wifi::*;

const WAIT: Duration = Duration::from_secs(3);

struct Shared {
    status: AtomicU8,
    finishes: AtomicU64,
    stop_failures: AtomicU8,
}

impl Shared {
    fn set_status(&self, status: ProviderStatus) {
        self.status.store(encode_status(status), Ordering::SeqCst);
    }
}

struct FakeWifi {
    shared: Arc<Shared>,
}

impl Wifi for FakeWifi {
    fn session(&self) -> SessionId {
        SessionId::new(7).unwrap()
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities {
            scan: true,
            open_network: true,
            wpa2_personal: true,
        }
    }
    fn status(&self) -> ProviderStatus {
        decode_status(self.shared.status.load(Ordering::SeqCst))
    }
    fn submit(&mut self, _: Request) -> Result<(), Rejected<Command, Error>> {
        Ok(())
    }
    fn request_stop(&mut self) -> Result<(), Error> {
        if self
            .shared
            .stop_failures
            .compare_exchange(1, 0, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            return Err(Error::Io);
        }
        self.shared.set_status(ProviderStatus::Stopped);
        Ok(())
    }
    fn finish(&mut self) -> Result<(), Error> {
        self.shared.finishes.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}

struct ReportSink(mpsc::SyncSender<OwnerReport>);

impl EventSink<OwnerReport> for ReportSink {
    fn try_emit(
        &mut self,
        report: OwnerReport,
    ) -> Result<(), Rejected<OwnerReport, DeliveryError>> {
        self.0.try_send(report).map_err(|error| match error {
            mpsc::TrySendError::Full(report) => Rejected {
                value: report,
                error: DeliveryError::Full,
            },
            mpsc::TrySendError::Disconnected(report) => Rejected {
                value: report,
                error: DeliveryError::Closed,
            },
        })
    }
}

struct RejectingSink;

impl EventSink<OwnerReport> for RejectingSink {
    fn try_emit(
        &mut self,
        report: OwnerReport,
    ) -> Result<(), Rejected<OwnerReport, DeliveryError>> {
        Err(Rejected {
            value: report,
            error: DeliveryError::Full,
        })
    }
}

#[test]
fn observer_failure_requires_explicit_stop_and_quiescence_before_finish() {
    let (mut owner, _stop, commands, _events, mut controller, shared) = fixture(1, 1);
    commands.try_submit(scan(), 100).unwrap();
    assert!(matches!(
        owner.run(&mut controller, &mut RejectingSink, || 0),
        Err(OwnerError::Observer(_))
    ));
    assert!(controller.is_busy());
    assert_eq!(controller.status(), ProviderStatus::Ready);
    assert_eq!(shared.finishes.load(Ordering::SeqCst), 0);

    controller.request_stop().unwrap();
    let (tx, _reports) = mpsc::sync_channel(4);
    owner
        .run(&mut controller, &mut ReportSink(tx), || 0)
        .unwrap();
    assert_eq!(controller.status(), ProviderStatus::Stopped);
    assert_eq!(shared.finishes.load(Ordering::SeqCst), 0);
    controller.finish().unwrap();
    assert_eq!(shared.finishes.load(Ordering::SeqCst), 1);
}

struct RejectTimeoutSink(mpsc::SyncSender<OwnerReport>);

impl EventSink<OwnerReport> for RejectTimeoutSink {
    fn try_emit(
        &mut self,
        report: OwnerReport,
    ) -> Result<(), Rejected<OwnerReport, DeliveryError>> {
        if matches!(
            &report,
            OwnerReport::Update(Update::Finished(outcome))
                if outcome.result == Err(Error::Timeout)
        ) {
            return Err(Rejected {
                value: report,
                error: DeliveryError::Full,
            });
        }
        let mut sink = ReportSink(self.0.clone());
        sink.try_emit(report)
    }
}

fn fixture(
    important_capacity: usize,
    ordinary_capacity: usize,
) -> (
    crate::OwnerLoop,
    crate::StopHandle,
    CommandSender,
    WifiEventSink,
    Controller<FakeWifi>,
    Arc<Shared>,
) {
    let shared = Arc::new(Shared {
        status: AtomicU8::new(encode_status(ProviderStatus::Ready)),
        finishes: AtomicU64::new(0),
        stop_failures: AtomicU8::new(0),
    });
    let (owner, stop, commands, events) = channels(important_capacity, ordinary_capacity).unwrap();
    let controller = Controller::new(FakeWifi {
        shared: shared.clone(),
    });
    (owner, stop, commands, events, controller, shared)
}

fn encode_status(status: ProviderStatus) -> u8 {
    match status {
        ProviderStatus::Starting => 0,
        ProviderStatus::Ready => 1,
        ProviderStatus::Stopping => 2,
        ProviderStatus::Stopped => 3,
        ProviderStatus::Failed(_) => 4,
    }
}

fn decode_status(status: u8) -> ProviderStatus {
    match status {
        0 => ProviderStatus::Starting,
        1 => ProviderStatus::Ready,
        2 => ProviderStatus::Stopping,
        3 => ProviderStatus::Stopped,
        _ => ProviderStatus::Failed(Error::Io),
    }
}

fn event(kind: EventKind) -> Event {
    Event {
        session: SessionId::new(7).unwrap(),
        kind,
    }
}

fn scan() -> Command {
    Command::Scan
}

fn spawn_owner(
    mut owner: crate::OwnerLoop,
    mut controller: Controller<FakeWifi>,
    start: Arc<Barrier>,
    reports: mpsc::SyncSender<OwnerReport>,
    now: Arc<AtomicU64>,
) -> thread::JoinHandle<(Result<(), OwnerError>, Controller<FakeWifi>)> {
    thread::spawn(move || {
        let mut observer = ReportSink(reports);
        start.wait();
        let result = owner.run(&mut controller, &mut observer, || {
            now.load(Ordering::SeqCst)
        });
        (result, controller)
    })
}

fn recv_report(receiver: &mpsc::Receiver<OwnerReport>) -> OwnerReport {
    receiver.recv_timeout(WAIT).expect("owner report timed out")
}

#[test]
fn cloned_sources_fan_in_and_scan_completion_stays_after_results() {
    let (owner, stop, commands, events, controller, _shared) = fixture(16, 4);
    let (report_tx, report_rx) = mpsc::sync_channel(32);
    let start = Arc::new(Barrier::new(2));
    let join = spawn_owner(
        owner,
        controller,
        start.clone(),
        report_tx,
        Arc::new(AtomicU64::new(0)),
    );
    start.wait();

    let (admitted_tx, admitted_rx) = mpsc::sync_channel(1);
    let command_clone = commands.clone();
    let command_thread = thread::spawn(move || {
        command_clone.try_submit(scan(), 100).unwrap();
        admitted_tx.send(()).unwrap();
    });
    admitted_rx.recv_timeout(WAIT).unwrap();
    command_thread.join().unwrap();
    let id = match recv_report(&report_rx) {
        OwnerReport::CommandAccepted(id) => id,
        other => panic!("unexpected report: {other:?}"),
    };

    let handoff = Arc::new(Barrier::new(2));
    let result_handoff = handoff.clone();
    let mut result_sink = events.clone();
    let result_thread = thread::spawn(move || {
        result_sink
            .try_emit(event(EventKind::ScanResult {
                operation: id.operation,
                access_point: AccessPoint {
                    ssid: None,
                    security: Security::Other,
                    rssi_dbm: Some(-42),
                },
            }))
            .unwrap();
        result_handoff.wait();
        result_handoff.wait();
    });
    let completion_handoff = handoff.clone();
    let mut completion_sink = events.clone();
    let completion_thread = thread::spawn(move || {
        completion_handoff.wait();
        completion_handoff.wait();
        completion_sink
            .try_emit(event(EventKind::Completed {
                operation: id.operation,
                result: Ok(Completion::Scan {
                    reported: 1,
                    truncated: false,
                }),
            }))
            .unwrap();
    });
    result_thread.join().unwrap();
    completion_thread.join().unwrap();

    let first = recv_report(&report_rx);
    let second = recv_report(&report_rx);
    assert!(matches!(first, OwnerReport::Update(Update::ScanResult(_))));
    assert!(matches!(second, OwnerReport::Update(Update::Finished(_))));

    stop.request_stop().unwrap();
    assert!(join.join().unwrap().0.is_ok());
}

#[test]
fn stop_admission_survives_both_full_traffic_queues() {
    let (owner, stop, _commands, mut events, controller, shared) = fixture(1, 1);
    events
        .try_emit(event(EventKind::LinkChanged(LinkState::Associated)))
        .unwrap();
    events
        .try_emit(event(EventKind::ScanResult {
            operation: OperationId::new(1).unwrap(),
            access_point: AccessPoint {
                ssid: None,
                security: Security::Other,
                rssi_dbm: None,
            },
        }))
        .unwrap();
    stop.request_stop().unwrap();
    assert_eq!(stop.request_stop(), Err(StopAdmissionError::AlreadyPending));

    let (report_tx, _report_rx) = mpsc::sync_channel(8);
    let start = Arc::new(Barrier::new(2));
    let join = spawn_owner(
        owner,
        controller,
        start.clone(),
        report_tx,
        Arc::new(AtomicU64::new(0)),
    );
    start.wait();
    let (result, mut controller) = join.join().unwrap();
    assert!(result.is_ok());
    assert_eq!(shared.finishes.load(Ordering::SeqCst), 0);
    controller.finish().unwrap();
    assert_eq!(shared.finishes.load(Ordering::SeqCst), 1);
}

#[test]
fn ordinary_completion_gets_a_turn_after_the_important_burst() {
    let (owner, stop, commands, mut events, controller, _) = fixture(16, 4);
    commands.try_submit(scan(), 100).unwrap();
    let operation = OperationId::new(1).unwrap();
    events
        .try_emit(event(EventKind::ScanResult {
            operation,
            access_point: AccessPoint {
                ssid: None,
                security: Security::Other,
                rssi_dbm: Some(-50),
            },
        }))
        .unwrap();
    events
        .try_emit(event(EventKind::Completed {
            operation,
            result: Ok(Completion::Scan {
                reported: 1,
                truncated: false,
            }),
        }))
        .unwrap();
    for state in [
        LinkState::Associated,
        LinkState::Addressed,
        LinkState::Disconnected,
        LinkState::Associated,
        LinkState::Addressed,
        LinkState::Disconnected,
        LinkState::Associated,
        LinkState::Addressed,
        LinkState::Disconnected,
    ] {
        events
            .try_emit(event(EventKind::LinkChanged(state)))
            .unwrap();
    }

    let (report_tx, report_rx) = mpsc::sync_channel(32);
    let start = Arc::new(Barrier::new(2));
    let join = spawn_owner(
        owner,
        controller,
        start.clone(),
        report_tx,
        Arc::new(AtomicU64::new(0)),
    );
    start.wait();
    let mut seen_scan_result = false;
    let mut links_before_scan_result = None;
    let mut completion_seen = false;
    let mut link_updates = 0;
    while !completion_seen {
        match recv_report(&report_rx) {
            OwnerReport::CommandAccepted(_) => {}
            OwnerReport::Update(Update::ScanResult(_)) => {
                seen_scan_result = true;
                links_before_scan_result = Some(link_updates);
            }
            OwnerReport::Update(Update::Finished(_)) => {
                assert!(seen_scan_result, "completion overtook scan results");
                completion_seen = true;
            }
            OwnerReport::State { .. } => link_updates += 1,
            OwnerReport::Update(update) => panic!("unexpected update: {update:?}"),
            OwnerReport::CommandRejected(rejected) => panic!("command rejected: {rejected:?}"),
        }
    }
    assert!(links_before_scan_result.unwrap() <= 7);
    stop.request_stop().unwrap();
    assert!(join.join().unwrap().0.is_ok());
}

#[test]
fn stop_endpoint_disconnect_requests_stop_and_closes_event_sinks() {
    let (mut owner, stop, commands, mut events, mut controller, _) = fixture(2, 2);
    drop(stop);
    let (report_tx, _report_rx) = mpsc::sync_channel(8);
    let mut observer = ReportSink(report_tx);
    owner.run(&mut controller, &mut observer, || 0).unwrap();
    assert_eq!(
        events.try_emit(event(EventKind::LinkChanged(LinkState::Associated))),
        Err(Rejected {
            value: event(EventKind::LinkChanged(LinkState::Associated)),
            error: DeliveryError::Closed,
        })
    );
    assert_eq!(
        events.try_emit(event(EventKind::ScanResult {
            operation: OperationId::new(1).unwrap(),
            access_point: AccessPoint {
                ssid: None,
                security: Security::Other,
                rssi_dbm: None,
            },
        })),
        Err(Rejected {
            value: event(EventKind::ScanResult {
                operation: OperationId::new(1).unwrap(),
                access_point: AccessPoint {
                    ssid: None,
                    security: Security::Other,
                    rssi_dbm: None,
                },
            }),
            error: DeliveryError::Closed,
        })
    );
    assert_eq!(
        commands.try_submit(scan(), 1).unwrap_err().error,
        DeliveryError::Closed
    );
    controller.finish().unwrap();
}

#[test]
fn observer_rejection_returns_report_and_keeps_owner_endpoints_open() {
    let (mut owner, stop, _commands, mut events, mut controller, _) = fixture(2, 2);
    events
        .try_emit(event(EventKind::LinkChanged(LinkState::Associated)))
        .unwrap();
    let error = owner
        .run(&mut controller, &mut RejectingSink, || 0)
        .unwrap_err();
    assert!(matches!(
        error,
        OwnerError::Observer(Rejected {
            value: OwnerReport::State {
                status: ProviderStatus::Ready,
                link: LinkState::Associated,
                fault: None,
            },
            error: DeliveryError::Full,
        })
    ));

    events
        .try_emit(event(EventKind::LinkChanged(LinkState::Addressed)))
        .unwrap();
    stop.request_stop().unwrap();
    let (report_tx, _report_rx) = mpsc::sync_channel(8);
    let mut observer = ReportSink(report_tx);
    owner.run(&mut controller, &mut observer, || 0).unwrap();
    controller.finish().unwrap();
}

#[test]
fn failed_stop_retains_loop_and_admission_for_retry() {
    let (mut owner, stop, _commands, mut events, mut controller, shared) = fixture(2, 2);
    shared.stop_failures.store(1, Ordering::SeqCst);
    stop.request_stop().unwrap();

    let (report_tx, _report_rx) = mpsc::sync_channel(8);
    let mut observer = ReportSink(report_tx);
    assert!(matches!(
        owner.run(&mut controller, &mut observer, || 0),
        Err(OwnerError::Stop(Error::Io))
    ));

    // The first stop message was consumed, but the live receivers remain
    // available so the lifecycle owner can request stop again and resume.
    events
        .try_emit(event(EventKind::LinkChanged(LinkState::Associated)))
        .unwrap();
    stop.request_stop().unwrap();
    owner.run(&mut controller, &mut observer, || 0).unwrap();
    controller.finish().unwrap();
}

#[test]
fn status_changes_report_coherent_status_link_and_fault_snapshots() {
    let (owner, _stop, _commands, mut events, controller, shared) = fixture(4, 4);
    events
        .try_emit(event(EventKind::Status(ProviderStatus::Stopping)))
        .unwrap();
    events
        .try_emit(event(EventKind::LinkChanged(LinkState::Associated)))
        .unwrap();

    let (report_tx, report_rx) = mpsc::sync_channel(8);
    let start = Arc::new(Barrier::new(2));
    let join = spawn_owner(
        owner,
        controller,
        start.clone(),
        report_tx,
        Arc::new(AtomicU64::new(0)),
    );
    start.wait();

    assert!(matches!(
        recv_report(&report_rx),
        OwnerReport::State {
            status: ProviderStatus::Stopping,
            link: LinkState::Unknown,
            fault: None,
        }
    ));
    assert!(matches!(
        recv_report(&report_rx),
        OwnerReport::State {
            status: ProviderStatus::Stopping,
            link: LinkState::Associated,
            fault: None,
        }
    ));

    shared.set_status(ProviderStatus::Stopped);
    events
        .try_emit(event(EventKind::Status(ProviderStatus::Stopped)))
        .unwrap();
    assert!(matches!(
        recv_report(&report_rx),
        OwnerReport::State {
            status: ProviderStatus::Stopped,
            link: LinkState::Unknown,
            fault: None,
        }
    ));
    assert!(join.join().unwrap().0.is_ok());
}

#[test]
fn provider_fault_snapshot_preserves_the_sticky_error_during_stop() {
    let (owner, _stop, _commands, mut events, controller, _) = fixture(4, 4);
    events
        .try_emit(event(EventKind::Status(ProviderStatus::Failed(
            Error::AuthenticationFailed,
        ))))
        .unwrap();

    let (report_tx, report_rx) = mpsc::sync_channel(8);
    let start = Arc::new(Barrier::new(2));
    let join = spawn_owner(
        owner,
        controller,
        start.clone(),
        report_tx,
        Arc::new(AtomicU64::new(0)),
    );
    start.wait();

    assert!(matches!(
        recv_report(&report_rx),
        OwnerReport::State {
            status: ProviderStatus::Failed(Error::AuthenticationFailed),
            link: LinkState::Unknown,
            fault: Some(Error::AuthenticationFailed),
        }
    ));
    assert!(matches!(
        recv_report(&report_rx),
        OwnerReport::State {
            status: ProviderStatus::Stopped,
            link: LinkState::Unknown,
            fault: Some(Error::AuthenticationFailed),
        }
    ));
    assert!(join.join().unwrap().0.is_ok());
}

#[test]
fn deadline_is_checked_on_wakeup_and_reported_before_that_event() {
    let (owner, stop, commands, mut events, controller, _) = fixture(4, 4);
    commands.try_submit(scan(), 5).unwrap();
    let (report_tx, report_rx) = mpsc::sync_channel(8);
    let now = Arc::new(AtomicU64::new(0));
    let start = Arc::new(Barrier::new(2));
    let join = spawn_owner(owner, controller, start.clone(), report_tx, now.clone());
    start.wait();
    let admitted = recv_report(&report_rx);
    assert!(matches!(admitted, OwnerReport::CommandAccepted(_)));

    now.store(5, Ordering::SeqCst);
    events
        .try_emit(event(EventKind::LinkChanged(LinkState::Associated)))
        .unwrap();
    let timeout = recv_report(&report_rx);
    let link = recv_report(&report_rx);
    assert!(matches!(timeout, OwnerReport::Update(Update::Finished(_))));
    assert!(matches!(
        link,
        OwnerReport::State {
            status: ProviderStatus::Ready,
            link: LinkState::Associated,
            fault: None,
        }
    ));

    stop.request_stop().unwrap();
    assert!(join.join().unwrap().0.is_ok());
}

#[test]
fn completion_selected_at_deadline_survives_timeout_observer_rejection() {
    let (mut owner, stop, commands, mut events, mut controller, _) = fixture(4, 4);
    commands.try_submit(scan(), 5).unwrap();
    let id = RequestId {
        session: SessionId::new(7).unwrap(),
        operation: OperationId::new(1).unwrap(),
    };
    events
        .try_emit(event(EventKind::Completed {
            operation: id.operation,
            result: Ok(Completion::Scan {
                reported: 0,
                truncated: false,
            }),
        }))
        .unwrap();

    let (report_tx, report_rx) = mpsc::sync_channel(8);
    let (first_result_tx, first_result_rx) = mpsc::sync_channel(1);
    let (resume_tx, resume_rx) = mpsc::sync_channel(1);
    let start = Arc::new(Barrier::new(2));
    let selected_completion = Arc::new(Barrier::new(2));
    let release_dispatch = Arc::new(Barrier::new(2));
    let now = Arc::new(AtomicU64::new(0));
    let clock_calls = Arc::new(AtomicU64::new(0));
    let join = thread::spawn({
        let start = start.clone();
        let selected_completion = selected_completion.clone();
        let release_dispatch = release_dispatch.clone();
        let now = now.clone();
        let clock_calls = clock_calls.clone();
        move || {
            let mut first_observer = RejectTimeoutSink(report_tx.clone());
            start.wait();
            let first_result = owner.run(&mut controller, &mut first_observer, || {
                if clock_calls.fetch_add(1, Ordering::SeqCst) == 3 {
                    // The command dispatch used two clock reads. Pause on the
                    // fourth, after ordinary completion was selected.
                    selected_completion.wait();
                    release_dispatch.wait();
                }
                now.load(Ordering::SeqCst)
            });
            let timeout_was_rejected = matches!(
                &first_result,
                Err(OwnerError::Observer(Rejected {
                    value: OwnerReport::Update(Update::Finished(outcome)),
                    error: DeliveryError::Full,
                })) if outcome.id == id && outcome.result == Err(Error::Timeout)
            );
            first_result_tx.send(timeout_was_rejected).unwrap();
            resume_rx.recv().unwrap();

            let mut observer = ReportSink(report_tx);
            let resumed_result = owner.run(&mut controller, &mut observer, || 5);
            (resumed_result, controller)
        }
    });
    start.wait();
    assert!(matches!(
        recv_report(&report_rx),
        OwnerReport::CommandAccepted(accepted) if accepted == id
    ));
    selected_completion.wait();
    now.store(5, Ordering::SeqCst);
    release_dispatch.wait();

    assert!(first_result_rx.recv_timeout(WAIT).unwrap());
    resume_tx.send(()).unwrap();
    assert!(matches!(
        recv_report(&report_rx),
        OwnerReport::Update(Update::Released(released)) if released == id
    ));
    stop.request_stop().unwrap();

    let (result, mut controller) = join.join().unwrap();
    assert!(result.is_ok());
    assert!(!controller.is_busy());
    controller.finish().unwrap();
}
