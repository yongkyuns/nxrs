//! Scripted packet acceptance for tests; not network delivery.
#![forbid(unsafe_code)]
use camera_test_support::packet::{DeviceError, PacketSink};
use std::collections::VecDeque;
use std::vec::Vec;

pub struct MockTransport {
    pub packets: Vec<Vec<u8>>,
    pub capacity: usize,
    pub send_errors: VecDeque<DeviceError>,
    pub sends: usize,
}

impl Default for MockTransport {
    fn default() -> Self {
        Self {
            packets: Vec::new(),
            capacity: 512,
            send_errors: VecDeque::new(),
            sends: 0,
        }
    }
}

impl PacketSink for MockTransport {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scripted_rejection_accepts_nothing_and_success_owns_the_packet() {
        let mut sender = MockTransport {
            capacity: 1,
            ..Default::default()
        };
        sender.send_errors.push_back(DeviceError::Busy);
        let mut bytes = [7, 9];
        assert_eq!(sender.send(&bytes), Err(DeviceError::Busy));
        assert!(sender.packets.is_empty());
        assert_eq!(sender.send(&bytes), Ok(()));
        bytes.fill(0);
        assert_eq!(sender.packets, vec![vec![7, 9]]);
        assert_eq!(sender.send(&bytes), Err(DeviceError::Full));
        assert_eq!(sender.packets.len(), 1);
        assert_eq!(sender.sends, 3);
    }
}
