//! Typed client driver. Backend ownership and calls stay entirely in Rust.
#![forbid(unsafe_code)]
use super::gatt_backend::{self as backend, Backend, Wake, CAPACITY};
use crate::gatt::{Command, Event, EventKind, Failure, Reply, Request, Value};
use crate::{gatt::Driver, Error, OperationId, Rejected, RequestId, SessionId};

pub(crate) trait PeerResolver {
    fn resolve(peer: crate::gatt::Peer) -> Result<Option<[u8; 16]>, Error>;
}
pub(crate) struct NativePeer;
impl PeerResolver for NativePeer {
    fn resolve(_: crate::gatt::Peer) -> Result<Option<[u8; 16]>, Error> {
        Ok(None)
    }
}
pub(crate) struct Native<const N: usize, B: Backend, P: PeerResolver = NativePeer> {
    session: SessionId,
    last_operation: Option<RequestId>,
    backend: core::marker::PhantomData<fn() -> (B, P)>,
}
pub(crate) fn open_with<
    const N: usize,
    B: Backend,
    P: PeerResolver,
    W: Fn() + Send + Sync + 'static,
>(
    wake: W,
) -> Result<Native<N, B, P>, Error> {
    if N > CAPACITY {
        return Err(Error::Unsupported);
    }
    let session = crate::worker::new_session()?;
    B::open(session, N, Wake::new(wake))?;
    Ok(Native {
        session,
        last_operation: None,
        backend: core::marker::PhantomData,
    })
}
impl<const N: usize, B: Backend, P: PeerResolver> Driver<N> for Native<N, B, P> {
    fn session(&self) -> SessionId {
        self.session
    }
    fn submit(&mut self, request: Request<N>) -> Result<(), Rejected<Request<N>, Error>> {
        let rejected = |error| Rejected {
            value: request,
            error,
        };
        if request.id.session != self.session {
            return Err(rejected(Error::InvalidValue));
        }
        let command = match request.command {
            Command::Connect { peer } => backend::Command::Connect {
                peer,
                identity: P::resolve(peer).map_err(rejected)?,
            },
            Command::Discover {
                service,
                characteristic,
            } => backend::Command::Discover {
                service,
                characteristic,
            },
            Command::Read { attribute } => backend::Command::Read {
                handle: attribute.handle().get(),
            },
            Command::Write { attribute, value } => backend::Command::Write {
                handle: attribute.handle().get(),
                value: Value::new(value.as_slice()).ok_or_else(|| rejected(Error::InvalidValue))?,
            },
            Command::Disconnect => backend::Command::Disconnect,
        };
        B::submit(
            self.session,
            backend::Request {
                operation: request.id.operation.get(),
                command,
            },
        )
        .map_err(rejected)?;
        self.last_operation = Some(request.id);
        Ok(())
    }
    fn recover(&mut self, recovery: RequestId) {
        if recovery.session == self.session {
            B::recover(self.session, recovery.operation);
        }
    }
    fn poll(&mut self) -> Option<Event<N>> {
        let raw = B::poll(self.session)?;
        let id = |operation| {
            OperationId::new(operation).map(|operation| RequestId {
                session: self.session,
                operation,
            })
        };
        let protocol = || EventKind::Completed {
            request: self.last_operation.unwrap_or(RequestId {
                session: self.session,
                operation: OperationId::new(1).unwrap(),
            }),
            result: Err(Failure::Protocol),
        };
        let kind = match raw {
            backend::Event::Completed { operation, result } => {
                if let Some(request) = id(operation) {
                    let result = result.and_then(|reply| {
                        Ok(match reply {
                            Reply::Connected => Reply::Connected,
                            Reply::Disconnected => Reply::Disconnected,
                            Reply::Written => Reply::Written,
                            Reply::Discovered { handle } => Reply::Discovered { handle },
                            Reply::Read(value) => {
                                Reply::Read(Value::new(value.as_slice()).ok_or(Failure::Protocol)?)
                            }
                        })
                    });
                    EventKind::Completed { request, result }
                } else {
                    protocol()
                }
            }
            backend::Event::Disconnected { connection, reason } => {
                id(connection).map_or_else(protocol, |connection| EventKind::Disconnected {
                    connection,
                    reason,
                })
            }
            backend::Event::Quiescent { recovery } => {
                id(recovery).map_or_else(protocol, |recovery| EventKind::Quiescent { recovery })
            }
            backend::Event::Fault => protocol(),
        };
        Some(Event {
            session: self.session,
            kind,
        })
    }
}
impl<const N: usize, B: Backend, P: PeerResolver> Drop for Native<N, B, P> {
    fn drop(&mut self) {
        B::close(self.session);
    }
}
#[cfg(test)]
pub(crate) mod tests;
