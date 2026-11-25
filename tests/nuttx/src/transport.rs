//! Core-only compatibility transport for the legacy NuttX integration fixture.
//!
//! The std fixture uses `std::net::UdpSocket`; this core-only fixture uses
//! its own scalar bridge.

use core::ffi::c_int;

use nuttx_support::{status, OwnedFd};
use camera_test_support::packet::{DeviceError, PacketSink};

extern "C" {
    fn rc_nx_udp_open(address: *const u8, port: u16) -> c_int;
    fn rc_nx_send(fd: c_int, bytes: *const u8, len: usize) -> c_int;
}

pub struct UdpSender {
    fd: OwnedFd,
}

impl UdpSender {
    pub fn connect(address: [u8; 4], port: u16) -> Result<Self, DeviceError> {
        // SAFETY: fixture bridge copies the four address octets synchronously.
        Ok(Self {
            fd: unsafe { OwnedFd::from_raw(rc_nx_udp_open(address.as_ptr(), port)) }
                .map_err(error)?,
        })
    }
}

impl PacketSink for UdpSender {
    fn send(&mut self, packet: &[u8]) -> Result<(), DeviceError> {
        if packet.len() > 65_507 {
            return Err(DeviceError::InvalidData);
        }
        // SAFETY: immutable packet storage remains live for the synchronous send.
        status(unsafe { rc_nx_send(self.fd.raw().map_err(error)?, packet.as_ptr(), packet.len()) })
            .map_err(error)
    }
}

fn error(code: c_int) -> DeviceError {
    match code {
        -1 => DeviceError::Unsupported,
        -2 => DeviceError::Busy,
        -3 => DeviceError::Timeout,
        -4 => DeviceError::Full,
        -5 => DeviceError::InvalidData,
        _ => DeviceError::Io,
    }
}
