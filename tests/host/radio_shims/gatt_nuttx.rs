use super::*;
fn code(e: Error) -> i32 {
    errno(match e {
        Error::InvalidValue => 1,
        Error::Busy => 2,
        Error::Io => 5,
        Error::Unavailable => 4,
        Error::Unsupported => 7,
        Error::PermissionDenied => 5,
        Error::Timeout => 5,
    })
}
fn disconnected() -> i32 {
    errno(3)
}
include!("gatt.rs");
pub(super) fn peer_lookup(peer: Peer, address: &mut [u8; 6], kind: &mut u8) -> Result<(), Error> {
    unsafe extern "C" {
        fn nxrs_ble_peer_lookup(session: u64, id: u64, address: *mut u8, kind: *mut u8) -> i32;
    }
    let result =
        unsafe { nxrs_ble_peer_lookup(peer.session.get(), peer.id.0, address.as_mut_ptr(), kind) };
    if result == 0 {
        Ok(())
    } else if result == errno(2) {
        Err(Error::Busy)
    } else {
        Err(Error::InvalidValue)
    }
}
