//! Wi-Fi station control, not sockets or a replacement network stack.
#![no_std]
#![forbid(unsafe_code)]

use core::fmt;
use hal_common::BoundedBytes;
pub use hal_common::{DeliveryError, EventSink, OperationId, Rejected, RequestId, SessionId};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    Busy,
    Unavailable,
    PermissionDenied,
    AuthenticationFailed,
    InvalidConfig,
    Timeout,
    Protocol,
    Io,
}

/// SSIDs are bytes, not necessarily UTF-8. Hidden scan results use None.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Ssid(BoundedBytes<32>);

impl Ssid {
    pub fn new(bytes: &[u8]) -> Result<Self, Error> {
        if bytes.is_empty() {
            return Err(Error::InvalidConfig);
        }
        BoundedBytes::new(bytes)
            .map(Self)
            .ok_or(Error::InvalidConfig)
    }

    pub fn as_bytes(&self) -> &[u8] {
        self.0.as_slice()
    }
}

/// WPA2 Personal passphrase, not a raw hexadecimal PSK. No Clone/Copy or
/// revealing Debug implementation. This is not a guaranteed-zeroization type.
pub struct Passphrase {
    bytes: BoundedBytes<63>,
}

impl Passphrase {
    pub fn new(value: &str) -> Result<Self, Error> {
        if !(8..=63).contains(&value.len())
            || !value.bytes().all(|byte| (0x20..=0x7e).contains(&byte))
        {
            return Err(Error::InvalidConfig);
        }
        Ok(Self {
            bytes: BoundedBytes::new(value.as_bytes()).unwrap(),
        })
    }

    /// Provider-only use when passing credentials to the native stack.
    pub fn as_bytes(&self) -> &[u8] {
        self.bytes.as_slice()
    }
}

impl fmt::Debug for Passphrase {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Passphrase([redacted])")
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Security {
    Open,
    Wpa2Personal,
    Other,
}

#[derive(Debug)]
pub enum Credentials {
    Open,
    Wpa2Personal(Passphrase),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Capabilities {
    pub scan: bool,
    pub open_network: bool,
    pub wpa2_personal: bool,
}

#[derive(Debug)]
pub enum Command {
    Scan,
    Connect {
        ssid: Ssid,
        credentials: Credentials,
    },
    Disconnect,
}

#[derive(Debug)]
pub struct Request {
    pub id: RequestId,
    pub command: Command,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AccessPoint {
    pub ssid: Option<Ssid>,
    pub security: Security,
    pub rssi_dbm: Option<i16>,
}

/// Addressed means usable IP configuration was observed, not Internet access.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LinkState {
    Unknown,
    Disconnected,
    Associated,
    Addressed,
}

/// Stopped guarantees callbacks are quiescent, not that a worker was joined.
/// Failed is sticky but does NOT guarantee quiescence. Explicit stop is needed.
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
    /// `truncated` also covers a bounded native source that cannot certify an
    /// exhaustive result. `reported` counts only admitted discovery items.
    Scan {
        reported: u32,
        truncated: bool,
    },
    Connected,
    Disconnected,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventKind {
    Status(ProviderStatus),
    LinkChanged(LinkState),
    ScanResult {
        operation: OperationId,
        access_point: AccessPoint,
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

/// Provider acquisition installs the typed sink before enabling callbacks.
/// All methods return promptly. Blocking OS calls belong to provider workers.
/// Link/status events are ordered within their traffic class. Scan results and
/// their completion must share one FIFO class; priority selection must not let
/// completion overtake results. Link snapshots remain authoritative even after
/// a caller's timeout. Admission Err accepts nothing and returns the original
/// owned command. The copyable request identity remains with the caller.
/// Each accepted operation emits one completion unless the session stops/fails.
/// No API here changes adapter power implicitly or persists credentials.
pub trait Wifi {
    /// Immutable for this object's lifetime, including stopping/failure.
    fn session(&self) -> SessionId;
    fn capabilities(&self) -> Capabilities;
    /// Sticky state permits recovery when a terminal event could not be queued.
    /// Providers must arrange a wakeup or retain rejected important events;
    /// callers must not have to periodically poll this snapshot.
    fn status(&self) -> ProviderStatus;
    fn submit(&mut self, request: Request) -> Result<(), Rejected<Command, Error>>;
    /// Independent of ordinary admission capacity; retryable on native failure.
    /// Stops this session, not unrelated OS-managed radio users. It does not
    /// imply disassociation; use the explicit Disconnect command for that.
    fn request_stop(&mut self) -> Result<(), Error>;
    /// Explicit teardown after Stopped. May block joining native workers;
    /// never call from normal service dispatch. On error resources stay owned.
    fn finish(&mut self) -> Result<(), Error>;
}

#[cfg(test)]
extern crate std;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ssids_are_bounded_bytes() {
        assert!(Ssid::new(&[]).is_err());
        assert!(Ssid::new(&[0; 33]).is_err());
        assert_eq!(Ssid::new(&[0xff, 0]).unwrap().as_bytes(), &[0xff, 0]);
    }

    #[test]
    fn credentials_validate_without_debug_leaks() {
        assert!(Passphrase::new("short").is_err());
        assert!(Passphrase::new(&"x".repeat(64)).is_err());
        assert!(Passphrase::new("1234567\n").is_err());
        let credentials = Credentials::Wpa2Personal(Passphrase::new("test-only-password").unwrap());
        assert!(!std::format!("{credentials:?}").contains("test-only-password"));
    }
}
