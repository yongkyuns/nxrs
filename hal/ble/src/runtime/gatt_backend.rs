//! Private Rust protocol between the client driver and native executors.
//! Only SDK callbacks cross an ABI. Commands, events and wake ownership do not.
#![allow(dead_code)] // A selected executor uses only its part of this private protocol.
use crate::{
    gatt::{Failure, Peer, Reply, Value},
    Error, OperationId, SessionId, Uuid,
};
use core::num::NonZeroU16;
use std::io;
use std::sync::Arc;
pub(crate) const CAPACITY: usize = 64;
#[derive(Clone)]
pub(crate) struct Wake(Arc<dyn Fn() + Send + Sync>);
impl Wake {
    pub fn new(wake: impl Fn() + Send + Sync + 'static) -> Self {
        Self(Arc::new(wake))
    }
    pub fn notify(&self) {
        (self.0)();
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Kind {
    Idle,
    Connect,
    Discover,
    Read,
    Write,
    Disconnect,
}
#[derive(Clone, Copy, Debug)]
pub(crate) enum Command {
    Idle,
    Connect {
        peer: Peer,
        identity: Option<[u8; 16]>,
    },
    Discover {
        service: Uuid,
        characteristic: Uuid,
    },
    Read {
        handle: u16,
    },
    Write {
        handle: u16,
        value: Value<CAPACITY>,
    },
    Disconnect,
}
#[derive(Clone, Copy, Debug)]
pub(crate) struct Request {
    pub operation: u64,
    pub command: Command,
}
impl Request {
    pub const IDLE: Self = Self {
        operation: 0,
        command: Command::Idle,
    };
    pub fn kind(self) -> Kind {
        match self.command {
            Command::Idle => Kind::Idle,
            Command::Connect { .. } => Kind::Connect,
            Command::Discover { .. } => Kind::Discover,
            Command::Read { .. } => Kind::Read,
            Command::Write { .. } => Kind::Write,
            Command::Disconnect => Kind::Disconnect,
        }
    }
    pub fn peer(self) -> Option<Peer> {
        if let Command::Connect { peer, .. } = self.command {
            Some(peer)
        } else {
            None
        }
    }
    pub fn identity(self) -> Option<[u8; 16]> {
        if let Command::Connect { identity, .. } = self.command {
            identity
        } else {
            None
        }
    }
    pub fn service(self) -> [u8; 16] {
        match self.command {
            Command::Discover { service, .. } => service.0,
            _ => [0; 16],
        }
    }
    pub fn characteristic(self) -> [u8; 16] {
        if let Command::Discover { characteristic, .. } = self.command {
            characteristic.0
        } else {
            [0; 16]
        }
    }
    pub fn handle(self) -> u16 {
        match self.command {
            Command::Read { handle } | Command::Write { handle, .. } => handle,
            _ => 0,
        }
    }
    pub fn value(&self) -> &[u8] {
        if let Command::Write { value, .. } = &self.command {
            value.as_slice()
        } else {
            &[]
        }
    }
}
#[derive(Clone, Copy, Debug)]
pub(crate) enum Event {
    Completed {
        operation: u64,
        result: Result<Reply<CAPACITY>, Failure>,
    },
    Disconnected {
        connection: u64,
        reason: Option<u8>,
    },
    Quiescent {
        recovery: u64,
    },
    Fault,
}
/// Accumulation of SDK read/discovery fields, never an inter-module wire format.
#[derive(Clone, Copy)]
pub(crate) struct Outcome {
    pub failure: Option<Failure>,
    pub handle: u16,
    pub length: usize,
    pub data: [u8; CAPACITY],
}
impl Outcome {
    pub const EMPTY: Self = Self {
        failure: None,
        handle: 0,
        length: 0,
        data: [0; CAPACITY],
    };
    pub fn native_error(&mut self, error: i32, att: u8, unsupported: i32) {
        self.failure = if error == unsupported {
            Some(Failure::Device(Error::Unsupported))
        } else if error != 0 {
            Some(native_failure(error))
        } else if att != 0 {
            Some(Failure::Att(att))
        } else {
            None
        };
    }
    pub fn finish(self, request: Request) -> Event {
        let result = if let Some(failure) = self.failure {
            Err(failure)
        } else {
            match request.kind() {
                Kind::Connect => Ok(Reply::Connected),
                Kind::Discover => NonZeroU16::new(self.handle)
                    .map(|handle| Reply::Discovered { handle })
                    .ok_or(Failure::Protocol),
                Kind::Read => self
                    .data
                    .get(..self.length)
                    .and_then(Value::new)
                    .map(Reply::Read)
                    .ok_or(Failure::Protocol),
                Kind::Write => Ok(Reply::Written),
                Kind::Disconnect => Ok(Reply::Disconnected),
                Kind::Idle => Err(Failure::Protocol),
            }
        };
        Event::Completed {
            operation: request.operation,
            result,
        }
    }
}
pub(crate) fn device_error(errno: i32) -> Error {
    match io::Error::from_raw_os_error(errno).kind() {
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted | io::ErrorKind::ResourceBusy => {
            Error::Busy
        }
        io::ErrorKind::NotFound | io::ErrorKind::NetworkDown | io::ErrorKind::NotConnected => {
            Error::Unavailable
        }
        io::ErrorKind::PermissionDenied => Error::PermissionDenied,
        io::ErrorKind::TimedOut => Error::Timeout,
        io::ErrorKind::InvalidInput
        | io::ErrorKind::InvalidData
        | io::ErrorKind::AlreadyExists
        | io::ErrorKind::FileTooLarge => Error::InvalidValue,
        io::ErrorKind::Unsupported => Error::Unsupported,
        _ => Error::Io,
    }
}
pub(crate) fn native_failure(errno: i32) -> Failure {
    match io::Error::from_raw_os_error(errno).kind() {
        io::ErrorKind::NotConnected
        | io::ErrorKind::ConnectionAborted
        | io::ErrorKind::ConnectionReset => Failure::Disconnected,
        _ => Failure::Device(device_error(errno)),
    }
}
pub(crate) trait Backend: 'static {
    fn open(session: SessionId, capacity: usize, wake: Wake) -> Result<(), Error>;
    fn submit(session: SessionId, request: Request) -> Result<(), Error>;
    fn poll(session: SessionId) -> Option<Event>;
    fn recover(session: SessionId, operation: OperationId);
    fn close(session: SessionId);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_header_unsupported_alias_does_not_depend_on_host_errno_tables() {
        let mut outcome = Outcome::EMPTY;
        // A target's ENOTSUP need not match the host std errno table.
        outcome.native_error(i32::MAX, 0, i32::MAX);
        assert_eq!(outcome.failure, Some(Failure::Device(Error::Unsupported)));
        outcome.native_error(0, 8, i32::MAX);
        assert_eq!(outcome.failure, Some(Failure::Att(8)));
        outcome.native_error(0, 0, i32::MAX);
        assert_eq!(outcome.failure, None);
    }
}
