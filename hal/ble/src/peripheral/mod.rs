//! One ordered, nonblocking byte stream per BLE central. The service UUIDs
//! identify the transport, while authentication belongs to the consumer.
use crate::{Error, Uuid};
use std::io::{Read, Write};
use std::time::Instant;

pub trait Link: Read + Write {
    fn abort(&mut self);
}

pub trait Listener {
    type Transport: Link;
    fn accept(&mut self) -> Result<Option<Self::Transport>, Error>;
    /// Whether the central has subscribed to the egress characteristic.
    fn notifying(&self) -> bool {
        false
    }
    /// Consume a readiness hint that may have woken the thread that opened
    /// this listener. The owner still drives retry deadlines.
    fn take_ready(&self) -> bool {
        false
    }
    /// The next retry needed while no accepted connection is active.
    fn idle_retry_at(&self) -> Option<Instant> {
        None
    }
    /// Progress bounded transport work from the owner's existing wait loop.
    fn service(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// UUIDs for one version of the byte-stream service, in display byte order.
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct Service {
    pub uuid: Uuid,
    pub ingress: Uuid,
    pub egress: Uuid,
}

/// Advertise one service with a write-with-response ingress and a read egress
/// characteristic. UUIDs use the conventional (display) byte order.
pub fn open(service: Uuid, ingress: Uuid, egress: Uuid) -> Result<impl Listener, Error> {
    crate::platform::open_peripheral(
        Service {
            uuid: service,
            ingress,
            egress,
        },
        None,
    )
}

/// Advertise a notification service while retaining the original read service
/// in the scan response. Both carry the same stream on one central connection.
/// Notifications prefix ciphertext with a little-endian 32-bit byte offset.
/// The ingress accepts acknowledged writes with a leading zero byte for data
/// and write commands with a leading one byte and the next contiguous offset
/// for cumulative acknowledgment. Unacknowledged bytes remain eligible for replay.
pub fn open_with_notifications(
    legacy: Service,
    notifications: Service,
) -> Result<impl Listener, Error> {
    crate::platform::open_peripheral(legacy, Some(notifications))
}
