//! Exercise the canonical application with a controlled device, not a second owner.
//! Gates make overload and shutdown deterministic; deadlines are test watchdogs.
use nxrs_controller_demo::{
    Command, Controller, INBOX_CAPACITY, Reply, Sink, StartError, State, Stopped, SubmitError,
    Ticket, parse_command,
};
use pulse::{Error, Event, Session, Stats, Waveform};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

const WATCHDOG: Duration = Duration::from_secs(10);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Operation {
    Configure(Waveform),
    Disable,
    Shutdown,
}

#[derive(Default)]
struct Record {
    calls: Vec<Operation>,
    stats: Stats,
    output: Option<Waveform>,
    dropped: bool,
}

struct Gate {
    entered: mpsc::SyncSender<()>,
    release: mpsc::Receiver<()>,
}

struct GateControl {
    entered: mpsc::Receiver<()>,
    release: mpsc::SyncSender<()>,
}

impl GateControl {
    fn wait(&self) {
        self.entered
            .recv_timeout(WATCHDOG)
            .expect("device did not reach its gate");
    }
    fn release(self) {
        self.release.try_send(()).expect("gate already left");
    }
}

#[derive(Clone, Default)]
struct Fixture {
    record: Arc<Mutex<Record>>,
    sink: Arc<Mutex<Option<Sink>>>,
}

impl Fixture {
    fn session(&self) -> TestSession {
        TestSession {
            fixture: self.clone(),
            gate: None,
            fail_at: None,
        }
    }

    fn emit(&self, sequence: u64, received: Instant) -> bool {
        self.record.lock().unwrap().stats.observed += 1;
        let accepted = self.sink.lock().unwrap().as_mut().expect("source closed")(Event::Edge {
            sequence,
            received,
        });
        if !accepted {
            self.record.lock().unwrap().stats.rejected += 1;
        }
        accepted
    }

    fn fault(&self, error: Error) -> bool {
        self.record.lock().unwrap().stats.fault = Some(error);
        self.sink.lock().unwrap().as_mut().expect("source closed")(Event::Fault(error))
    }
}

struct TestSession {
    fixture: Fixture,
    gate: Option<(usize, Gate)>,
    fail_at: Option<usize>,
}

impl TestSession {
    fn gated(mut self, at: usize) -> (Self, GateControl) {
        let (entered_tx, entered_rx) = mpsc::sync_channel(1);
        let (release_tx, release_rx) = mpsc::sync_channel(1);
        self.gate = Some((
            at,
            Gate {
                entered: entered_tx,
                release: release_rx,
            },
        ));
        (
            self,
            GateControl {
                entered: entered_rx,
                release: release_tx,
            },
        )
    }

    fn call(&mut self, operation: Operation) -> Result<(), Error> {
        let count = {
            let mut record = self.fixture.record.lock().unwrap();
            record.calls.push(operation);
            record.output = match operation {
                Operation::Configure(wave) => Some(wave),
                Operation::Disable | Operation::Shutdown => None,
            };
            record.calls.len()
        };
        if self.gate.as_ref().is_some_and(|(at, _)| *at == count) {
            let (_, gate) = self.gate.take().unwrap();
            gate.entered.try_send(()).unwrap();
            gate.release.recv_timeout(WATCHDOG).map_err(|_| Error::Io)?;
        }
        // This fixture applies the write before failing: callers cannot infer
        // electrical state from a failed operation.
        if self.fail_at == Some(count) {
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
}

impl Session for TestSession {
    fn configure(&mut self, wave: Waveform) -> Result<(), Error> {
        self.call(Operation::Configure(wave))
    }
    fn disable(&mut self) -> Result<(), Error> {
        self.call(Operation::Disable)
    }
    fn stats(&self) -> Stats {
        self.fixture.record.lock().unwrap().stats
    }
    fn shutdown(&mut self) -> Result<Stats, Error> {
        let result = self.call(Operation::Shutdown);
        self.fixture.sink.lock().unwrap().take();
        result?;
        let stats = self.stats();
        stats.fault.map_or(Ok(stats), Err)
    }
}

impl Drop for TestSession {
    fn drop(&mut self) {
        self.fixture.record.lock().unwrap().dropped = true;
        self.fixture.sink.lock().unwrap().take();
    }
}

fn start(session: TestSession) -> Controller {
    let fixture = session.fixture.clone();
    Controller::start(move |sink| {
        *fixture.sink.lock().unwrap() = Some(sink);
        Ok(session)
    })
    .expect("start failed")
}

fn submit(controller: &Controller, command: Command) -> Ticket {
    controller
        .try_command(command)
        .unwrap_or_else(|_| panic!("unexpected rejection: {command:?}"))
}

fn within_deadline<T: Send + 'static>(operation: impl FnOnce() -> T + Send + 'static) -> T {
    let (tx, rx) = mpsc::sync_channel(1);
    let worker = thread::spawn(move || {
        let _ = tx.send(operation());
    });
    let result = rx
        .recv_timeout(WATCHDOG)
        .expect("test operation did not complete");
    worker.join().expect("test helper panicked");
    result
}

fn reply(ticket: Ticket) -> Reply {
    within_deadline(move || ticket.wait().expect("admitted command lost its completion"))
}

fn stop(controller: Controller) -> Stopped {
    within_deadline(move || controller.stop().expect("owner panicked"))
}

fn wave(hz: u32, duty: u8) -> Waveform {
    Waveform::new(hz, duty).unwrap()
}

#[test]
fn parser_checks_waveform_ranges_and_rejects_extra_arguments() {
    for (input, expected) in [
        ("on", Command::On),
        (" off ", Command::Off),
        ("status", Command::Status),
        ("stats", Command::Stats),
        ("set 50 75", Command::Set(wave(50, 75))),
    ] {
        assert_eq!(parse_command(input), Ok(expected));
    }
    for input in [
        "",
        "stop",
        "on extra",
        "set",
        "set 25",
        "set 0 50",
        "set 9 50",
        "set 101 50",
        "set 25 0",
        "set 25 100",
        "set 25 50 trailing",
        "set -1 20",
    ] {
        assert!(parse_command(input).is_err(), "accepted {input:?}");
    }
}

#[test]
fn commands_have_completions_and_on_restores_last_selected_waveform() {
    let fixture = Fixture::default();
    let controller = start(fixture.session());
    assert_eq!(
        reply(submit(&controller, Command::Status)).unwrap().state,
        State::Off
    );
    assert_eq!(
        reply(submit(&controller, Command::On)).unwrap().state,
        State::Running(wave(25, 25))
    );
    assert_eq!(
        reply(submit(&controller, Command::Set(wave(50, 75))))
            .unwrap()
            .state,
        State::Running(wave(50, 75))
    );
    assert_eq!(
        reply(submit(&controller, Command::Off)).unwrap().state,
        State::Off
    );
    assert_eq!(
        reply(submit(&controller, Command::On)).unwrap().state,
        State::Running(wave(50, 75))
    );
    let result = stop(controller);
    assert_eq!(result.status.state, State::Off);
    assert!(result.shutdown.is_ok());
    assert_eq!(result.status.completed_commands, 5);
    let record = fixture.record.lock().unwrap();
    assert_eq!(
        record.calls,
        [
            Operation::Configure(wave(25, 25)),
            Operation::Configure(wave(50, 75)),
            Operation::Disable,
            Operation::Configure(wave(50, 75)),
            Operation::Shutdown
        ]
    );
    assert!(record.dropped);
}

#[test]
fn acquisition_failure_does_not_start_an_owner() {
    let result = Controller::start(|_| Err::<TestSession, _>(Error::Busy));
    assert!(matches!(result, Err(StartError::Device(Error::Busy))));
}

#[test]
fn invalid_watchdog_timeout_has_no_acquisition_effects() {
    for timeout in [
        Duration::ZERO,
        Duration::from_millis(999),
        Duration::from_secs(61),
    ] {
        let result = Controller::start_with_watchdog(
            |_| -> Result<TestSession, Error> { panic!("invalid timeout acquired a device") },
            Some(timeout),
        );
        assert!(matches!(
            result,
            Err(StartError::Watchdog {
                error: watchdog::DeviceError::InvalidTimeout,
                cleanup: None,
            })
        ));
    }
}

#[test]
fn full_inbox_returns_the_command_and_stop_drains_accepted_work() {
    let fixture = Fixture::default();
    let (session, gate) = fixture.session().gated(1);
    let controller = start(session);
    let first = submit(&controller, Command::On);
    gate.wait();
    let queued: Vec<_> = (0..INBOX_CAPACITY)
        .map(|_| submit(&controller, Command::Status))
        .collect();
    assert!(matches!(
        controller.try_command(Command::Off),
        Err(SubmitError::Full(Command::Off))
    ));
    let stopper = thread::spawn(move || stop(controller));
    gate.release();
    // Reply slots are not consumed until AFTER stop joins. Source retains its
    // callback until shutdown; that Weak callback must not keep recv alive.
    let stopped = stopper.join().unwrap();
    assert!(stopped.shutdown.is_ok());
    assert_eq!(reply(first).unwrap().state, State::Running(wave(25, 25)));
    for ticket in queued {
        assert_eq!(reply(ticket).unwrap().state, State::Running(wave(25, 25)));
    }
    assert_eq!(stopped.status.completed_commands, INBOX_CAPACITY as u64 + 1);
}

#[test]
fn discarded_reply_does_not_block_later_commands_or_shutdown() {
    let fixture = Fixture::default();
    let (session, gate) = fixture.session().gated(1);
    let controller = start(session);
    let abandoned = submit(&controller, Command::On);
    gate.wait();
    drop(abandoned);
    let status = submit(&controller, Command::Status);
    gate.release();
    let stopped = stop(controller);
    assert_eq!(reply(status).unwrap().state, State::Running(wave(25, 25)));
    assert!(stopped.shutdown.is_ok());
}

#[test]
fn failed_configuration_reports_unknown_until_an_explicit_success() {
    let fixture = Fixture::default();
    let mut session = fixture.session();
    session.fail_at = Some(1);
    let controller = start(session);
    assert_eq!(reply(submit(&controller, Command::On)), Err(Error::Io));
    assert_eq!(
        reply(submit(&controller, Command::Status)).unwrap().state,
        State::Unknown(Error::Io)
    );
    assert_eq!(
        reply(submit(&controller, Command::Off)).unwrap().state,
        State::Off
    );
    assert!(stop(controller).shutdown.is_ok());
}

#[test]
fn failed_shutdown_never_reports_output_off() {
    let fixture = Fixture::default();
    let mut session = fixture.session();
    session.fail_at = Some(2);
    let controller = start(session);
    reply(submit(&controller, Command::On)).unwrap();
    let stopped = stop(controller);
    assert_eq!(stopped.shutdown, Err(Error::Io));
    assert_eq!(stopped.status.state, State::Unknown(Error::Io));
}

#[test]
fn task_receipts_measure_dispatch_and_sequence_gaps_without_inventing_irq_time() {
    let fixture = Fixture::default();
    let controller = start(fixture.session());
    let first = Instant::now() - Duration::from_millis(10);
    assert!(fixture.emit(1, first));
    assert!(fixture.emit(4, first + Duration::from_millis(5)));
    let status = reply(submit(&controller, Command::Stats)).unwrap();
    assert_eq!(status.observations.processed, 2);
    assert_eq!(status.observations.sequence_gaps, 2);
    assert_eq!(
        status.observations.last_interval,
        Some(Duration::from_millis(5))
    );
    assert!(status.observations.max_dispatch >= Duration::from_millis(10));
    assert!(stop(controller).shutdown.is_ok());
}

#[test]
fn saturated_source_counts_rejection_and_retains_a_lost_fault() {
    let fixture = Fixture::default();
    let (session, gate) = fixture.session().gated(1);
    let controller = start(session);
    let first = submit(&controller, Command::On);
    gate.wait();
    for index in 1..=INBOX_CAPACITY {
        assert!(fixture.emit(index as u64, Instant::now()));
    }
    assert!(!fixture.emit(INBOX_CAPACITY as u64 + 1, Instant::now()));
    assert!(!fixture.fault(Error::Io));
    let stopper = thread::spawn(move || stop(controller));
    gate.release();
    let result = stopper.join().unwrap();
    let _ = reply(first);
    assert_eq!(result.status.source.observed, INBOX_CAPACITY as u64 + 1);
    assert_eq!(result.status.source.rejected, 1);
    assert_eq!(result.status.observations.processed, INBOX_CAPACITY as u64);
    assert_eq!(result.shutdown, Err(Error::Io));
    assert_eq!(result.status.state, State::Unknown(Error::Io));
}

#[test]
fn source_fault_stays_visible_and_prevents_a_new_output_start() {
    let fixture = Fixture::default();
    let controller = start(fixture.session());
    assert!(fixture.fault(Error::Io));
    let status = reply(submit(&controller, Command::Status)).unwrap();
    assert_eq!(status.source.fault, Some(Error::Io));
    assert_eq!(status.state, State::Unknown(Error::Io));
    assert_eq!(reply(submit(&controller, Command::On)), Err(Error::Io));
    assert_eq!(
        reply(submit(&controller, Command::Off)).unwrap().state,
        State::Off
    );
    assert_eq!(stop(controller).shutdown, Err(Error::Io));
    assert_eq!(
        fixture.record.lock().unwrap().calls,
        [Operation::Disable, Operation::Shutdown]
    );
}

#[test]
fn repeated_controller_sessions_release_their_sources() {
    let fixture = Fixture::default();
    for _ in 0..32 {
        let controller = start(fixture.session());
        reply(submit(&controller, Command::On)).unwrap();
        assert!(stop(controller).shutdown.is_ok());
        assert!(fixture.sink.lock().unwrap().is_none());
    }
    assert_eq!(fixture.record.lock().unwrap().calls.len(), 64);
}
