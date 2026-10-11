//! BLE central operations and portable events; no controller addresses or OS
//! handles. Peripheral/GATT-server contracts follow in a separate qualified slice.
#![forbid(unsafe_code)]

pub use hal_common::{
    BoundedBytes, DeliveryError, EventSink, OperationId, Rejected, RequestId, SessionId,
};

/// Provider-issued, session-local identity; not a Bluetooth MAC address.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PeerId(pub u64);

/// Canonical 128-bit UUID in network byte order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Uuid(pub [u8; 16]);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ServiceId(pub u64);

/// Session/peer-local instance identity. UUIDs alone do not identify duplicate
/// service or characteristic instances; IDs are not native ATT handles.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CharacteristicId(pub u64);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    Busy,
    Unavailable,
    PermissionDenied,
    InvalidPeer,
    InvalidValue,
    AuthenticationRequired,
    Disconnected,
    Timeout,
    Io,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Capabilities {
    /// True only when the provider qualifies central/GATT operations. A scanner
    /// may advertise `scan` while leaving this false.
    pub central: bool,
    /// True when the provider supports LE discovery reports. NuttX's native
    /// stack buffers only a finite number of reports (typically 8 or 32), so
    /// results are best-effort and may be lost; this is not GATT qualification.
    pub scan: bool,
    /// Maximum single-operation value length; negotiated peer limits may be lower.
    pub max_value_len: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WriteMode {
    WithResponse,
    WithoutResponse,
}

/// Capacity N is a product resource choice, not an assumed negotiated ATT MTU.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Command<const N: usize> {
    StartScan {
        service: Option<Uuid>,
    },
    StopScan,
    Connect(PeerId),
    Disconnect(PeerId),
    DiscoverServices(PeerId),
    DiscoverCharacteristics {
        peer: PeerId,
        service: ServiceId,
    },
    Read {
        peer: PeerId,
        characteristic: CharacteristicId,
    },
    Write {
        peer: PeerId,
        characteristic: CharacteristicId,
        mode: WriteMode,
        value: BoundedBytes<N>,
    },
    Subscribe {
        peer: PeerId,
        characteristic: CharacteristicId,
        enabled: bool,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Request<const N: usize> {
    pub id: RequestId,
    pub command: Command<N>,
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
pub enum Completion<const N: usize> {
    ScanStarted,
    ScanStopped,
    Connected(PeerId),
    Disconnected(PeerId),
    Discovery {
        reported: u32,
        truncated: bool,
    },
    Read(BoundedBytes<N>),
    /// WithoutResponse is local-stack acceptance, not remote processing proof.
    Written(WriteMode),
    SubscriptionChanged,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventKind<const N: usize> {
    Status(ProviderStatus),
    Discovered {
        peer: PeerId,
        rssi_dbm: Option<i16>,
    },
    Disconnected {
        peer: PeerId,
        reason: Error,
    },
    Service {
        operation: OperationId,
        peer: PeerId,
        service: ServiceId,
        uuid: Uuid,
    },
    Characteristic {
        operation: OperationId,
        peer: PeerId,
        service: ServiceId,
        characteristic: CharacteristicId,
        uuid: Uuid,
    },
    Value {
        peer: PeerId,
        characteristic: CharacteristicId,
        value: BoundedBytes<N>,
    },
    Completed {
        operation: OperationId,
        result: Result<Completion<N>, Error>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Event<const N: usize> {
    pub session: SessionId,
    pub kind: EventKind<N>,
}

/// Methods admit work without waiting for a radio response. Sink installation,
/// rejection ownership, sticky failure and quiescent-stop rules match Wi-Fi.
/// Providers enforce current peer/value limits before accepting a request.
/// Instance IDs must not be reused after disconnection in the same session.
/// Validate every ID against the current session/peer: constructing an ID does
/// not grant access. Unknown IDs must fail without invoking a native operation.
/// Discovery items and their completion share a FIFO traffic class, so priority
/// selection cannot deliver completion ahead of accepted discovery results.
/// Oversized values must never be silently truncated or implicitly fragmented.
/// No automatic retry of a write: it could duplicate a remote side effect.
pub trait Ble<const N: usize> {
    /// Immutable for the provider lifetime. Restart requires a fresh session.
    fn session(&self) -> SessionId;
    fn capabilities(&self) -> Capabilities;
    fn status(&self) -> ProviderStatus;
    /// First terminal failure, retained even after Stopped and worker teardown.
    /// A dropped status event must not turn failed shutdown into success.
    fn fault(&self) -> Option<Error>;
    fn submit(&mut self, request: Request<N>) -> Result<(), Rejected<Request<N>, Error>>;
    fn request_stop(&mut self) -> Result<(), Error>;
    /// Explicit, possibly blocking worker reclamation after quiescent Stopped.
    /// On failure retain resources for retry; do not perform this in dispatch.
    fn finish(&mut self) -> Result<(), Error>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn duplicate_uuids_have_distinct_instance_ids() {
        let uuid = Uuid([1; 16]);
        let a = EventKind::<20>::Characteristic {
            operation: OperationId::new(1).unwrap(),
            peer: PeerId(1),
            service: ServiceId(1),
            characteristic: CharacteristicId(1),
            uuid,
        };
        let b = EventKind::<20>::Characteristic {
            operation: OperationId::new(1).unwrap(),
            peer: PeerId(1),
            service: ServiceId(1),
            characteristic: CharacteristicId(2),
            uuid,
        };
        assert_ne!(a, b);
    }

    #[test]
    fn values_are_bounded_without_assuming_a_peer_mtu() {
        assert!(BoundedBytes::<20>::new(&[0; 21]).is_none());
        assert_eq!(
            BoundedBytes::<20>::new(&[0; 20]).unwrap().as_slice().len(),
            20
        );
        assert!(BoundedBytes::<20>::new(&[]).unwrap().as_slice().is_empty());
    }
}
