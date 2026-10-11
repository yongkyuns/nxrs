//! Portable BLE scan API; no controller addresses or OS handles.
#![forbid(unsafe_code)]

pub use hal_common::{DeliveryError, EventSink, OperationId, Rejected, RequestId, SessionId};

/// Provider-issued, session-local identity; not a Bluetooth MAC address.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PeerId(pub u64);

/// Canonical 128-bit UUID in network byte order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Uuid(pub [u8; 16]);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    Busy,
    Unavailable,
    PermissionDenied,
    InvalidValue,
    Timeout,
    Io,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Capabilities {
    pub scan: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Command {
    StartScan { service: Option<Uuid> },
    StopScan,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Request {
    pub id: RequestId,
    pub command: Command,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProviderStatus {
    Starting,
    Ready,
    Stopping,
    Stopped,
    Failed(Error),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Completion {
    ScanStarted,
    ScanStopped,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventKind {
    Status(ProviderStatus),
    Discovered {
        peer: PeerId,
        rssi_dbm: Option<i16>,
    },
    /// The bounded peer table filled during this scan; further new peers are omitted.
    DiscoveryLimited {
        limit: u16,
    },
    Completed {
        operation: OperationId,
        result: Result<Completion, Error>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Event {
    pub session: SessionId,
    pub kind: EventKind,
}

/// Nonblocking scan admission and quiescent-stop contract. A timed-out request
/// retains its operation slot until native completion or provider shutdown.
pub trait Ble {
    fn session(&self) -> SessionId;
    fn capabilities(&self) -> Capabilities;
    fn status(&self) -> ProviderStatus;
    /// First terminal failure, retained independently of the current status.
    fn fault(&self) -> Option<Error>;
    fn submit(&mut self, request: Request) -> Result<(), Rejected<Request, Error>>;
    fn request_stop(&mut self) -> Result<(), Error>;
    /// Reclaim worker resources after Stopped confirms callbacks are quiescent.
    fn finish(&mut self) -> Result<(), Error>;
}
