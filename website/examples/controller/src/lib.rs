//! One application owner for commands and task-context pulse observations.
//!
//! The same owner runs with the explicitly selected mock and NuttX providers.
//! Admission never waits for inbox space. Each accepted command has one reply
//! slot; discarding a ticket cannot block the owner. Call stop to drain
//! admitted work, stop acquisition, and join. Drop closes admission and detaches.
#![forbid(unsafe_code)]

use pulse::{Error, Event, Session, Stats, Waveform};
use std::fmt;
use std::io;
use std::sync::mpsc::{self, Receiver, RecvError, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

mod supervision;
use supervision::{CHECK_INTERVAL, Supervisor};

pub const INBOX_CAPACITY: usize = 16;
pub type Sink = Box<dyn FnMut(Event) -> bool + Send>;

// tutorial:controller-commands:start
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Command {
    On,
    Off,
    Set(Waveform),
    Status,
    Stats,
}

/// Last successfully requested output state, not electrical readback.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Off,
    Running(Waveform),
    Unknown(Error),
}
// tutorial:controller-commands:end

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ParseError;

pub fn parse_command(text: &str) -> Result<Command, ParseError> {
    let mut words = text.split_whitespace();
    let command = match words.next() {
        Some("on") => Command::On,
        Some("off") => Command::Off,
        Some("status") => Command::Status,
        Some("stats") => Command::Stats,
        Some("set") => {
            let frequency = words
                .next()
                .ok_or(ParseError)?
                .parse()
                .map_err(|_| ParseError)?;
            let duty = words
                .next()
                .ok_or(ParseError)?
                .parse()
                .map_err(|_| ParseError)?;
            Command::Set(Waveform::new(frequency, duty).map_err(|_| ParseError)?)
        }
        _ => return Err(ParseError),
    };
    if words.next().is_some() {
        return Err(ParseError);
    }
    Ok(command)
}

impl fmt::Display for Command {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::On => f.write_str("on"),
            Self::Off => f.write_str("off"),
            Self::Set(wave) => write!(f, "set {} {}", wave.frequency_hz(), wave.duty_percent()),
            Self::Status => f.write_str("status"),
            Self::Stats => f.write_str("stats"),
        }
    }
}

impl fmt::Display for State {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Off => f.write_str("off"),
            Self::Running(wave) => write!(
                f,
                "on ({} Hz, {}%)",
                wave.frequency_hz(),
                wave.duty_percent()
            ),
            Self::Unknown(error) => write!(f, "unknown ({error:?})"),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Observations {
    pub processed: u64,
    pub sequence_gaps: u64,
    /// Spacing between the last two task-context source receipts.
    pub last_interval: Option<Duration>,
    /// Largest observed source-receipt to application-dispatch delay.
    pub max_dispatch: Duration,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Status {
    pub state: State,
    pub source: Stats,
    pub observations: Observations,
    pub completed_commands: u64,
    pub watchdog_fault: Option<watchdog::DeviceError>,
}

pub type Reply = Result<Status, Error>;

struct Request {
    command: Command,
    reply: SyncSender<Reply>,
}

enum Message {
    Command(Request),
    Source(Event),
}

#[must_use = "wait for completion or explicitly discard the ticket"]
pub struct Ticket(Receiver<Reply>);

impl Ticket {
    /// A disconnected ticket means no completion was delivered. It does not
    /// imply that the output operation had no effect.
    pub fn wait(self) -> Result<Reply, RecvError> {
        self.0.recv()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SubmitError {
    Full(Command),
    Disconnected(Command),
}

#[derive(Debug)]
pub enum StartError {
    Device(Error),
    Spawn {
        error: io::Error,
        cleanup: Option<Error>,
    },
    Watchdog {
        error: watchdog::DeviceError,
        cleanup: Option<Error>,
    },
}

impl fmt::Display for StartError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Device(error) => write!(f, "cannot acquire pulse device: {error:?}"),
            Self::Spawn { error, cleanup } => write!(
                f,
                "cannot start controller worker: {error}; pulse cleanup: {cleanup:?}"
            ),
            Self::Watchdog { error, cleanup } => write!(
                f,
                "cannot arm watchdog: {error:?}; pulse cleanup: {cleanup:?}"
            ),
        }
    }
}

impl std::error::Error for StartError {}

pub struct Stopped {
    pub status: Status,
    /// Explicit acquisition shutdown result, including retained source faults.
    pub shutdown: Result<Stats, Error>,
    pub watchdog: Result<(), watchdog::DeviceError>,
}

/// Deliberately not Clone. The controller owns the only persistent inbox sender.
#[must_use = "call stop to close admission, drain accepted work, and join"]
pub struct Controller {
    commands: Arc<SyncSender<Message>>,
    worker: JoinHandle<Stopped>,
}

impl Controller {
    pub fn start<S: Session>(
        open: impl FnOnce(Sink) -> Result<S, Error>,
    ) -> Result<Self, StartError> {
        Self::start_with_watchdog(open, None)
    }

    pub fn start_with_watchdog<S: Session>(
        open: impl FnOnce(Sink) -> Result<S, Error>,
        timeout: Option<Duration>,
    ) -> Result<Self, StartError> {
        if timeout
            .is_some_and(|value| value < Duration::from_secs(1) || value > Duration::from_secs(60))
        {
            return Err(StartError::Watchdog {
                error: watchdog::DeviceError::InvalidTimeout,
                cleanup: None,
            });
        }
        // tutorial:controller-start:start
        let (sender, incoming) = mpsc::sync_channel(INBOX_CAPACITY);
        let commands = Arc::new(sender);
        let source = Arc::downgrade(&commands);
        let session = open(Box::new(move |event| {
            source
                .upgrade()
                .is_some_and(|sender| sender.try_send(Message::Source(event)).is_ok())
        }))
        .map_err(StartError::Device)?;
        // tutorial:controller-start:end
        // Keep acquisition available for explicit rollback if thread creation
        // fails. This handoff is used only at startup, never in the event loop.
        let acquired = Arc::new(Mutex::new(Some(session)));
        let handoff = acquired.clone();
        let (ready, started) = mpsc::sync_channel(1);
        let spawn = thread::Builder::new()
            .name("controller".into())
            .stack_size(16 * 1024)
            .spawn(move || {
                let session = handoff.lock().unwrap().take().expect("one startup owner");
                // Arm only on the actual owner, after source acquisition and
                // thread creation succeeded. The caller observes startup failure.
                let device = timeout.map(watchdog::arm).transpose();
                let _ = ready.try_send(device.as_ref().map(|_| ()).map_err(|error| *error));
                own_session(session, incoming, device.unwrap_or(None))
            });
        let worker = match spawn {
            Ok(worker) => worker,
            Err(error) => {
                let mut session = acquired.lock().unwrap().take().expect("unstarted owner");
                let cleanup = session.shutdown().err();
                return Err(StartError::Spawn { error, cleanup });
            }
        };
        match started.recv() {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                drop(commands);
                let cleanup = worker
                    .join()
                    .map_or(Some(Error::Shutdown), |stopped| stopped.shutdown.err());
                return Err(StartError::Watchdog { error, cleanup });
            }
            Err(_) => {
                drop(commands);
                let _ = worker.join();
                return Err(StartError::Spawn {
                    error: io::Error::other("controller startup failed"),
                    cleanup: Some(Error::Shutdown),
                });
            }
        }
        Ok(Self { commands, worker })
    }

    // tutorial:controller-admit:start
    pub fn try_command(&self, command: Command) -> Result<Ticket, SubmitError> {
        let (reply, completion) = mpsc::sync_channel(1);
        match self
            .commands
            .try_send(Message::Command(Request { command, reply }))
        {
            Ok(()) => Ok(Ticket(completion)),
            Err(TrySendError::Full(Message::Command(request))) => {
                Err(SubmitError::Full(request.command))
            }
            Err(TrySendError::Disconnected(Message::Command(request))) => {
                Err(SubmitError::Disconnected(request.command))
            }
            _ => unreachable!("only the submitted command can be returned"),
        }
    }
    // tutorial:controller-admit:end

    // tutorial:controller-stop:start
    /// Close admission, drain accepted commands/observations, stop acquisition,
    /// and join. Native operations must return; this is not a hard time bound.
    pub fn stop(self) -> thread::Result<Stopped> {
        drop(self.commands);
        self.worker.join()
    }
    // tutorial:controller-stop:end
}

fn own_session<S: Session, W: watchdog::RuntimeWatchdog>(
    mut session: S,
    incoming: Receiver<Message>,
    watchdog: Option<W>,
) -> Stopped {
    let mut state = State::Off;
    let mut waveform = Waveform::new(25, 25).expect("supported default waveform");
    let mut observations = Observations::default();
    let mut last_receipt = None;
    let mut last_sequence = None;
    let mut completed_commands = 0u64;
    let mut configured = Instant::now();
    let mut watchdog = Supervisor::new(watchdog);
    // tutorial:controller-owner:start
    loop {
        let message = if watchdog.enabled() {
            match incoming.recv_timeout(CHECK_INTERVAL) {
                Ok(message) => message,
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    let now = Instant::now();
                    watchdog.check(
                        now,
                        session.stats().fault.is_none()
                            && supervision::healthy(state, configured, last_receipt, now),
                    );
                    continue;
                }
            }
        } else {
            match incoming.recv() {
                Ok(message) => message,
                Err(_) => break,
            }
        };
        match message {
            Message::Source(Event::Edge { sequence, received }) => {
                observations.processed = observations.processed.saturating_add(1);
                observations.max_dispatch = observations
                    .max_dispatch
                    .max(Instant::now().saturating_duration_since(received));
                observations.last_interval =
                    last_receipt.and_then(|previous| received.checked_duration_since(previous));
                observations.sequence_gaps = observations.sequence_gaps.saturating_add(
                    last_sequence.map_or(sequence.saturating_sub(1), |previous: u64| {
                        sequence.saturating_sub(previous.saturating_add(1))
                    }),
                );
                last_receipt = Some(received);
                last_sequence = Some(sequence);
            }
            Message::Source(Event::Fault(error)) => state = State::Unknown(error),
            Message::Command(request) => {
                completed_commands = completed_commands.saturating_add(1);
                let source = session.stats();
                if let Some(error) = source.fault {
                    state = State::Unknown(error);
                }
                let operation = match request.command {
                    Command::On | Command::Set(_) if source.fault.is_some() => {
                        Err(source.fault.unwrap())
                    }
                    Command::On => session.configure(waveform).map(|()| {
                        if !matches!(state, State::Running(_)) {
                            configured = Instant::now();
                        }
                        State::Running(waveform)
                    }),
                    Command::Set(next) => session.configure(next).map(|()| {
                        waveform = next;
                        // Reconfiguring a running output is not evidence of
                        // source progress and must not extend its grace period.
                        if !matches!(state, State::Running(_)) {
                            configured = Instant::now();
                        }
                        State::Running(next)
                    }),
                    Command::Off => session.disable().map(|()| State::Off),
                    Command::Status | Command::Stats => Ok(state),
                };
                let reply = match operation {
                    Ok(next) => {
                        state = next;
                        Ok(Status {
                            state,
                            source: session.stats(),
                            observations,
                            completed_commands,
                            watchdog_fault: watchdog.fault(),
                        })
                    }
                    Err(error) => {
                        state = State::Unknown(error);
                        Err(error)
                    }
                };
                // A discarded ticket never prevents later work or shutdown.
                let _ = request.reply.try_send(reply);
            }
        }
        let now = Instant::now();
        watchdog.check(
            now,
            session.stats().fault.is_none()
                && supervision::healthy(state, configured, last_receipt, now),
        );
    }
    // tutorial:controller-owner:end
    let shutdown = session.shutdown();
    let watchdog_result = watchdog.finish(shutdown.is_ok());
    state = match shutdown {
        Ok(_) => State::Off,
        Err(error) => State::Unknown(error),
    };
    Stopped {
        status: Status {
            state,
            source: session.stats(),
            observations,
            completed_commands,
            watchdog_fault: watchdog.fault(),
        },
        shutdown,
        watchdog: watchdog_result,
    }
}
