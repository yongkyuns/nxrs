//! Admission and constructor wiring; all socket behavior stays in crate::udp.
#[cfg(any(hal_mock, test))]
#[cfg_attr(test, allow(dead_code))]
mod mock;
#[cfg(hal_mock)]
mod selected {
    pub use super::mock::MockTransport;
    pub fn open() -> impl crate::Transport {
        MockTransport::default()
    }
}
#[cfg(hal_enabled)]
mod selected {
    #[cfg(not(target_os = "nuttx"))]
    pub use crate::udp::transport as open;
    pub use crate::udp::{transport, UdpSender, UdpTransport};
    #[cfg(target_os = "nuttx")]
    pub fn open(address: [u8; 4], port: u16) -> Result<impl crate::Transport, crate::DeviceError> {
        UdpSender::connect(address, port)
    }
}
#[cfg(any(hal_enabled, hal_mock))]
pub use selected::*;

#[cfg(all(
    not(hal_enabled),
    any(
        target_os = "linux",
        target_os = "macos",
        target_os = "windows",
        target_os = "nuttx"
    )
))]
mod unbound;
#[cfg(all(
    not(hal_enabled),
    any(
        target_os = "linux",
        target_os = "macos",
        target_os = "windows",
        target_os = "nuttx"
    )
))]
pub use unbound::*;
