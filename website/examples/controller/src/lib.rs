//! A small application owner using an existing capability and ordinary Rust.
//!
//! The queue bounds waiting commands. Each accepted command has one completion
//! slot; callers own their tickets and should consume or discard them promptly.
//! Call `Controller::stop` to observe shutdown and join the worker. Dropping a
//! controller closes admission but detaches its thread, as with a `JoinHandle`.
#![forbid(unsafe_code)]

use led::{DeviceError, Led};
use std::fmt;
use std::io;
use std::sync::mpsc::{self, Receiver, RecvError, SyncSender, TrySendError};
use std::thread::{self, JoinHandle};

pub const COMMAND_CAPACITY: usize = 2;

// tutorial:controller-commands:start
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Command {
    Set(bool),
    Status,
}

/// Last successfully commanded value, never electrical readback.
/// A failed write can have changed the output, so its value becomes unknown.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Off,
    On,
    Unknown(DeviceError),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ParseError;

pub fn parse_command(text: &str) -> Result<Command, ParseError> {
    match text.trim() {
        "on" => Ok(Command::Set(true)),
        "off" => Ok(Command::Set(false)),
        "status" => Ok(Command::Status),
        _ => Err(ParseError),
    }
}
// tutorial:controller-commands:end

impl fmt::Display for Command {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Set(true) => "on",
            Self::Set(false) => "off",
            Self::Status => "status",
        })
    }
}

impl fmt::Display for State {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Off => f.write_str("off"),
            Self::On => f.write_str("on"),
            Self::Unknown(error) => write!(f, "unknown ({error:?})"),
        }
    }
}

pub type Reply = Result<State, DeviceError>;

struct Request {
    command: Command,
    reply: SyncSender<Reply>,
}

#[must_use = "wait for completion or explicitly discard the ticket"]
pub struct Ticket(Receiver<Reply>);

impl Ticket {
    /// Outer failure means the worker disappeared without delivering a result.
    /// An inner error is the output provider's reported command failure.
    pub fn wait(self) -> Result<Reply, RecvError> {
        self.0.recv()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SubmitError {
    Full(Command),
    Disconnected(Command),
}

/// Initialization failures keep the output available for the caller to recover.
/// On thread creation failure, initialization has already set the output off;
/// the unused thread closure releases that resource without starting a worker.
pub enum StartError<L> {
    Output { output: L, error: DeviceError },
    Spawn(io::Error),
}

impl<L> fmt::Debug for StartError<L> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Output { error, .. } => f
                .debug_struct("Output")
                .field("error", error)
                .finish_non_exhaustive(),
            Self::Spawn(error) => f.debug_tuple("Spawn").field(error).finish(),
        }
    }
}

impl<L> fmt::Display for StartError<L> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Output { error, .. } => write!(f, "cannot initialize output: {error:?}"),
            Self::Spawn(error) => write!(f, "cannot start controller worker: {error}"),
        }
    }
}

impl<L> std::error::Error for StartError<L> {}

/// The worker has joined. The caller retains the output even if switching it
/// off failed, and can choose recovery according to the application's policy.
pub struct Stopped<L> {
    pub output: L,
    pub state: State,
    pub shutdown: Result<(), DeviceError>,
}

/// This handle is deliberately not Clone and exposes no command sender.
#[must_use = "call stop to close admission, drain accepted work, and join"]
pub struct Controller<L> {
    commands: SyncSender<Request>,
    worker: JoinHandle<Stopped<L>>,
}

impl<L: Led + Send + 'static> Controller<L> {
    // tutorial:controller-start:start
    pub fn start(mut output: L, index: u8) -> Result<Self, StartError<L>> {
        if let Err(error) = output.supported().validate(index) {
            return Err(StartError::Output { output, error });
        }
        if let Err(error) = output.set(index, false) {
            return Err(StartError::Output { output, error });
        }
        let (commands, incoming) = mpsc::sync_channel(COMMAND_CAPACITY);
        let worker = thread::Builder::new()
            .name("controller".into())
            .spawn(move || own_output(output, index, incoming))
            .map_err(StartError::Spawn)?;
        Ok(Self { commands, worker })
    }
    // tutorial:controller-start:end

    // tutorial:controller-admit:start
    pub fn try_command(&self, command: Command) -> Result<Ticket, SubmitError> {
        let (reply, completion) = mpsc::sync_channel(1);
        match self.commands.try_send(Request { command, reply }) {
            Ok(()) => Ok(Ticket(completion)),
            Err(TrySendError::Full(request)) => Err(SubmitError::Full(request.command)),
            Err(TrySendError::Disconnected(request)) => {
                Err(SubmitError::Disconnected(request.command))
            }
        }
    }
    // tutorial:controller-admit:end

    // tutorial:controller-stop:start
    /// Drain admitted commands, attempt output-off, and join. This depends on
    /// synchronous Led::set calls returning; it is not a shutdown deadline.
    pub fn stop(self) -> thread::Result<Stopped<L>> {
        drop(self.commands);
        self.worker.join()
    }
    // tutorial:controller-stop:end
}

// tutorial:controller-owner:start
fn own_output<L: Led>(mut output: L, index: u8, incoming: Receiver<Request>) -> Stopped<L> {
    let mut state = State::Off;
    while let Ok(request) = incoming.recv() {
        let result = match request.command {
            Command::Status => Ok(state),
            Command::Set(on) => match output.set(index, on) {
                Ok(()) => {
                    state = if on { State::On } else { State::Off };
                    Ok(state)
                }
                Err(error) => {
                    state = State::Unknown(error);
                    Err(error)
                }
            },
        };
        // One result, one slot. A caller discarding a ticket cannot block us.
        let _ = request.reply.try_send(result);
    }
    let shutdown = output.set(index, false);
    state = match shutdown {
        Ok(()) => State::Off,
        Err(error) => State::Unknown(error),
    };
    Stopped {
        output,
        state,
        shutdown,
    }
}
// tutorial:controller-owner:end
