//! A packet-output boundary for portable telemetry, not a socket or thread API.
#![no_std]
#![forbid(unsafe_code)]

pub use hal_common::DeviceError;

/// Submit one complete packet to an output selected by the caller.
///
/// `Ok(())` means local acceptance only, not remote delivery. `Err` must accept
/// nothing: it must not leave a partially submitted packet to be retried.
/// The borrowed bytes are valid only during this call. An asynchronous output
/// must copy into owned, bounded storage before returning success.
///
/// Event-loop outputs must return without waiting for a peer or future capacity.
/// `Busy` requests a later retry according to the owner's readiness/deadline
/// policy; it is not permission to spin. Maximum packet size and empty-packet
/// support belong to the selected output's documented contract.
///
/// This interface is optional dependency injection for packet-producing logic.
/// Use std::net directly for a service whose contract is specifically UDP.
/// Internal thread communication uses channels, not this trait.
pub trait PacketSink {
    fn send(&mut self, packet: &[u8]) -> Result<(), DeviceError>;
}

/// A local callback is sufficient when an output does not need its own type.
impl<F> PacketSink for F
where
    F: FnMut(&[u8]) -> Result<(), DeviceError>,
{
    fn send(&mut self, packet: &[u8]) -> Result<(), DeviceError> {
        self(packet)
    }
}

// Compatibility name for existing core-only fixtures. This is the same trait,
// not a second interface or a blanket adapter between competing abstractions.
pub use PacketSink as Transport;
