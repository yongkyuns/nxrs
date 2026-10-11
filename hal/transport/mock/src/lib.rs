//! Scripted packet acceptance for tests; not network delivery.
#![forbid(unsafe_code)]
use std::collections::VecDeque;
use transport_api::{DeviceError, Transport};

pub struct MockTransport {
    pub packets: Vec<Vec<u8>>,
    pub capacity: usize,
    pub send_errors: VecDeque<DeviceError>,
    pub sends: usize,
}

impl Default for MockTransport {
    fn default() -> Self {
        Self { packets: Vec::new(), capacity: 512, send_errors: VecDeque::new(), sends: 0 }
    }
}

impl Transport for MockTransport {
    fn send(&mut self, packet: &[u8]) -> Result<(), DeviceError> {
        self.sends += 1;
        if let Some(error) = self.send_errors.pop_front() {
            return Err(error);
        }
        if self.packets.len() == self.capacity {
            return Err(DeviceError::Full);
        }
        self.packets.push(packet.to_vec());
        Ok(())
    }
}
