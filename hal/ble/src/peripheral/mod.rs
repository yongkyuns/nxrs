//! One ordered, nonblocking byte stream per BLE central. The service UUIDs
//! identify the transport, while authentication belongs to the consumer.
use crate::{Error, Uuid};
use std::io::{Read, Write};

pub trait Link: Read + Write {
    fn abort(&mut self);
}

pub trait Listener {
    type Transport: Link;
    fn accept(&mut self) -> Result<Option<Self::Transport>, Error>;
}

/// Advertise one service with a write-with-response ingress and a read egress
/// characteristic. UUIDs use the conventional (display) byte order.
pub fn open(service: Uuid, ingress: Uuid, egress: Uuid) -> Result<impl Listener, Error> {
    crate::platform::open_peripheral(service, ingress, egress)
}
