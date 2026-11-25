//! Application decisions and ownership, using the existing LED contract.
//!
//! Gates establish queue saturation without scheduler sleeps. Their bounded
//! waits are test watchdogs, not claims about physical device response times.

use led::{DeviceError, Led, LedSet};
use nxrs_controller_demo::{
    COMMAND_CAPACITY, Command, Controller, Reply, StartError, State, Stopped, SubmitError, Ticket,
    parse_command,
};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::thread;
use std::time::Duration;

const WATCHDOG: Duration = Duration::from_secs(10);

#[derive(Debug)]
struct Gate {
    entered: SyncSender<()>,
    release: Receiver<()>,
}

struct GateControl {
    entered: Receiver<()>,
    release: SyncSender<()>,
}

impl GateControl {
    fn wait_until_blocked(&self) {
        self.entered
            .recv_timeout(WATCHDOG)
            .expect("the fixture did not reach its controlled operation");
    }

    fn release(self) {
        self.release
            .try_send(())
            .expect("the fixture stopped waiting before it was released");
    }
}

#[derive(Debug)]
struct TestLed {
    supported: LedSet,
    on: bool,
    calls: Vec<(u8, bool)>,
    fail_at: Option<(usize, DeviceError)>,
    gate_at: Option<(usize, Gate)>,
}

impl Default for TestLed {
    fn default() -> Self {
        Self {
            supported: LedSet::from_bits(1),
            // Startup must establish the requested initial state explicitly.
            on: true,
            calls: Vec::new(),
            fail_at: None,
            gate_at: None,
        }
    }
}

impl TestLed {
    fn gated(call: usize) -> (Self, GateControl) {
        let (entered_tx, entered_rx) = mpsc::sync_channel(1);
        let (release_tx, release_rx) = mpsc::sync_channel(1);
        (
            Self {
                gate_at: Some((
                    call,
                    Gate {
                        entered: entered_tx,
                        release: release_rx,
                    },
                )),
                ..Self::default()
            },
            GateControl {
                entered: entered_rx,
                release: release_tx,
            },
        )
    }
}

impl Led for TestLed {
    fn supported(&self) -> LedSet {
        self.supported
    }

    fn set(&mut self, index: u8, on: bool) -> Result<(), DeviceError> {
        self.calls.push((index, on));
        self.supported.validate(index)?;
        // A failing operation can still have an effect. The public LED
        // contract does not promise to roll an output back on an error.
        self.on = on;
        if self
            .gate_at
            .as_ref()
            .is_some_and(|(call, _)| *call == self.calls.len())
        {
            let (_, gate) = self.gate_at.take().unwrap();
            gate.entered.try_send(()).map_err(|_| DeviceError::Io)?;
            // If the test fails, dropping GateControl disconnects this wait.
            gate.release
                .recv_timeout(WATCHDOG)
                .map_err(|_| DeviceError::Timeout)?;
        }
        if let Some((call, error)) = self.fail_at
            && call == self.calls.len()
        {
            return Err(error);
        }
        Ok(())
    }
}

fn start(output: TestLed) -> Controller<TestLed> {
    match Controller::start(output, 0) {
        Ok(controller) => controller,
        Err(StartError::Output { error, .. }) => panic!("unexpected startup error: {error:?}"),
        Err(StartError::Spawn(error)) => panic!("could not start the controller: {error}"),
    }
}

fn submit(controller: &Controller<TestLed>, command: Command) -> Ticket {
    match controller.try_command(command) {
        Ok(ticket) => ticket,
        Err(_) => panic!("unexpected rejection of {command:?}"),
    }
}

fn within_deadline<T: Send + 'static>(operation: impl FnOnce() -> T + Send + 'static) -> T {
    let (finished_tx, finished_rx) = mpsc::sync_channel(1);
    let helper = thread::spawn(move || {
        let _ = finished_tx.send(operation());
    });
    let result = finished_rx
        .recv_timeout(WATCHDOG)
        .expect("application operation did not finish before the test watchdog");
    helper.join().expect("test helper panicked");
    result
}

fn reply(ticket: Ticket) -> Reply {
    within_deadline(move || ticket.wait().expect("accepted request lost its reply"))
}

fn stop(controller: Controller<TestLed>) -> Stopped<TestLed> {
    within_deadline(move || controller.stop().expect("controller worker panicked"))
}

#[test]
fn parser_accepts_only_the_documented_commands() {
    for (text, command) in [
        ("on", Command::Set(true)),
        ("off", Command::Set(false)),
        ("status", Command::Status),
    ] {
        assert_eq!(parse_command(text).ok(), Some(command));
    }
    for text in ["", "toggle", "1", "set", "on off", "status extra"] {
        assert!(
            parse_command(text).is_err(),
            "accepted invalid input {text:?}"
        );
    }
}

#[test]
fn startup_and_completed_commands_establish_the_reported_state() {
    let controller = start(TestLed::default());
    assert_eq!(reply(submit(&controller, Command::Status)), Ok(State::Off));
    assert_eq!(
        reply(submit(&controller, Command::Set(true))),
        Ok(State::On)
    );
    assert_eq!(reply(submit(&controller, Command::Status)), Ok(State::On));
    assert_eq!(
        reply(submit(&controller, Command::Set(false))),
        Ok(State::Off)
    );

    let stopped = stop(controller);
    assert_eq!(stopped.shutdown, Ok(()));
    assert_eq!(stopped.state, State::Off);
    assert!(!stopped.output.on);
    assert_eq!(
        stopped.output.calls,
        [(0, false), (0, true), (0, false), (0, false)]
    );
}

#[test]
fn unsupported_and_out_of_range_indices_do_not_enter_the_provider() {
    for (mask, index, expected) in [
        (0b10, 0, DeviceError::Unsupported),
        (1, 32, DeviceError::InvalidData),
        (1, u8::MAX, DeviceError::InvalidData),
    ] {
        let output = TestLed {
            supported: LedSet::from_bits(mask),
            ..TestLed::default()
        };
        match Controller::start(output, index) {
            Err(StartError::Output { output, error }) => {
                assert_eq!(error, expected);
                assert!(output.calls.is_empty());
                assert!(output.on);
            }
            Err(StartError::Spawn(error)) => panic!("unexpected spawn attempt: {error}"),
            Ok(controller) => {
                let _ = stop(controller);
                panic!("an invalid logical index was admitted");
            }
        }
    }
}

#[test]
fn startup_failure_returns_the_output_for_recovery() {
    let output = TestLed {
        fail_at: Some((1, DeviceError::Io)),
        ..TestLed::default()
    };
    let output = match Controller::start(output, 0) {
        Err(StartError::Output { output, error }) => {
            assert_eq!(error, DeviceError::Io);
            assert_eq!(output.calls, [(0, false)]);
            output
        }
        Err(StartError::Spawn(error)) => panic!("unexpected spawn attempt: {error}"),
        Ok(controller) => {
            let _ = stop(controller);
            panic!("startup ignored an output failure");
        }
    };
    // The transient failure has passed; the caller still owns this same value.
    let stopped = stop(start(output));
    assert_eq!(stopped.shutdown, Ok(()));
    assert_eq!(stopped.output.calls, [(0, false), (0, false), (0, false)]);
}

#[test]
fn full_queue_rejects_the_original_command_and_stop_drains_admitted_work() {
    let (output, gate) = TestLed::gated(2);
    let controller = start(output);
    let first = submit(&controller, Command::Set(true));
    gate.wait_until_blocked();

    let queued: Vec<_> = (0..COMMAND_CAPACITY)
        .map(|slot| {
            submit(
                &controller,
                if slot == 0 {
                    Command::Set(false)
                } else {
                    Command::Status
                },
            )
        })
        .collect();
    match controller.try_command(Command::Set(true)) {
        Err(SubmitError::Full(command)) => assert_eq!(command, Command::Set(true)),
        Err(SubmitError::Disconnected(_)) => panic!("the live worker disconnected"),
        Ok(_) => panic!("the saturated queue admitted another request"),
    }

    let (stopping_tx, stopping_rx) = mpsc::sync_channel(1);
    let (stopped_tx, stopped_rx) = mpsc::sync_channel(1);
    let stopper = thread::spawn(move || {
        stopping_tx.send(()).unwrap();
        let _ = stopped_tx.send(controller.stop());
    });
    stopping_rx.recv_timeout(WATCHDOG).unwrap();
    gate.release();
    // No completion is consumed until after stop joins the owner. Reply
    // backpressure must not form a cycle with shutdown.
    let stopped = stopped_rx
        .recv_timeout(WATCHDOG)
        .expect("stop did not drain the full queue")
        .expect("controller worker panicked");
    stopper.join().unwrap();
    assert_eq!(reply(first), Ok(State::On));
    for ticket in queued {
        assert_eq!(reply(ticket), Ok(State::Off));
    }
    assert_eq!(stopped.shutdown, Ok(()));
    assert_eq!(stopped.state, State::Off);
    assert_eq!(
        stopped.output.calls,
        [(0, false), (0, true), (0, false), (0, false)]
    );
}

#[test]
fn an_abandoned_ticket_does_not_block_later_commands_or_join() {
    let (output, gate) = TestLed::gated(2);
    let controller = start(output);
    let abandoned = submit(&controller, Command::Set(true));
    gate.wait_until_blocked();
    drop(abandoned);
    let status = submit(&controller, Command::Status);
    gate.release();

    let stopped = stop(controller);
    assert_eq!(reply(status), Ok(State::On));
    assert_eq!(stopped.shutdown, Ok(()));
    assert!(!stopped.output.on);
}

#[test]
fn a_failed_operation_is_unknown_until_a_later_set_succeeds() {
    let controller = start(TestLed {
        fail_at: Some((2, DeviceError::Io)),
        ..TestLed::default()
    });
    assert_eq!(
        reply(submit(&controller, Command::Set(true))),
        Err(DeviceError::Io)
    );
    assert_eq!(
        reply(submit(&controller, Command::Status)),
        Ok(State::Unknown(DeviceError::Io))
    );
    assert_eq!(
        reply(submit(&controller, Command::Set(false))),
        Ok(State::Off)
    );
    assert_eq!(reply(submit(&controller, Command::Status)), Ok(State::Off));

    let stopped = stop(controller);
    assert_eq!(stopped.shutdown, Ok(()));
    assert!(!stopped.output.on);
}

#[test]
fn failed_shutdown_reports_uncertainty_and_returns_the_output() {
    let controller = start(TestLed {
        fail_at: Some((3, DeviceError::Io)),
        ..TestLed::default()
    });
    assert_eq!(
        reply(submit(&controller, Command::Set(true))),
        Ok(State::On)
    );

    let stopped = stop(controller);
    assert_eq!(stopped.shutdown, Err(DeviceError::Io));
    assert_eq!(stopped.state, State::Unknown(DeviceError::Io));
    // This fixture changed the output before failing. Its effect is visible
    // to this test, but cannot be inferred by a generic Led caller.
    assert!(!stopped.output.on);
    let recovered = stop(start(stopped.output));
    assert_eq!(recovered.shutdown, Ok(()));
    assert_eq!(recovered.state, State::Off);
}

#[test]
fn a_joined_output_can_start_a_fresh_controller() {
    let first = start(TestLed::default());
    assert_eq!(reply(submit(&first, Command::Set(true))), Ok(State::On));
    let first = stop(first);

    let second = start(first.output);
    assert_eq!(reply(submit(&second, Command::Status)), Ok(State::Off));
    assert_eq!(reply(submit(&second, Command::Set(true))), Ok(State::On));
    let second = stop(second);
    assert_eq!(second.shutdown, Ok(()));
    assert_eq!(second.state, State::Off);
    assert_eq!(
        second.output.calls,
        [
            (0, false),
            (0, true),
            (0, false),
            (0, false),
            (0, true),
            (0, false)
        ]
    );
}
