//! One ordered, nonblocking byte stream per BLE central. The service UUIDs
//! identify the transport, while authentication belongs to the consumer.
use crate::{Error, Uuid};
use std::io::{Read, Write};
use std::time::Instant;

pub trait Link: Read + Write {
    fn abort(&mut self);
}

/// Delivery evidence for one accepted transport. Compare tokens for equality;
/// neither token is a wall clock or an application message sequence.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DeliveryProgress {
    /// Changes when a different central connection is admitted.
    pub generation: u32,
    /// Changes only as queued output is consumed by the transport's peer-facing
    /// delivery operation. Enqueueing and retransmission are not progress.
    /// NuttX advances this on a validated cumulative notification ACK or on a
    /// legacy ATT read taking bytes. It does not prove application processing
    /// or durable storage, and wraps within a generation.
    pub completed: u32,
    /// Output is retained awaiting transport delivery, including queued bytes
    /// not yet sent. False does not describe a consumer's own pending writes.
    pub pending: bool,
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
    /// A coherent delivery snapshot for the accepted connection. None means the
    /// provider cannot expose this evidence; local writes alone must not be
    /// labelled peer acknowledgment. WouldBlock means retry the snapshot, not
    /// that the output queue is empty or the peer has stopped progressing.
    fn delivery_progress(&self) -> std::io::Result<Option<DeliveryProgress>> {
        Ok(None)
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
