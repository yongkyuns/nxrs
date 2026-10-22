//! OS acquisition wiring; all socket behavior lives in crate::udp.
#[cfg(not(target_os = "nuttx"))]
pub use crate::udp::transport as open;
#[cfg(target_os = "nuttx")]
pub fn open(address: [u8; 4], port: u16) -> Result<impl crate::Transport, crate::DeviceError> {
    crate::udp::UdpSender::connect(address, port)
}
