//! Shared std networking is separate from profile-selected transport acquisition.
use std::io;

#[test]
fn explicit_udp_constructor_is_available_without_hardware_admission() {
    let result = transport::udp::transport("not-a-numeric-socket-address");
    assert_eq!(result.err().expect("non-numeric peer is invalid").kind(), io::ErrorKind::InvalidInput);
}
