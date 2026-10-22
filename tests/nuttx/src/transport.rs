//! Core-only compatibility transport for the legacy NuttX integration fixture.
//!
//! Production NuttX packet output uses `hal/transport/src/udp.rs`, a
//! normal `std::net::UdpSocket` provider. This module intentionally keeps the
//! old scalar C bridge local to the no_std fixture until that fixture itself
//! migrates to the ordinary-std application path.

use core::ffi::c_int;

use nuttx_support::{status, OwnedFd};
use transport::{DeviceError, Transport};

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

impl Transport for UdpSender {
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
