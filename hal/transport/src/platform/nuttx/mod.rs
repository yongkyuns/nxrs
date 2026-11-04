pub use crate::udp::{transport, UdpSender, UdpTransport};
pub fn open(address: [u8; 4], port: u16) -> Result<impl crate::Transport, crate::DeviceError> {
    UdpSender::connect(address, port)
}
