#![cfg(hal_mock)]

use transport::{DeviceError, MockTransport, Transport};

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
