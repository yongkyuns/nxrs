//! BlueZ daemon-scoped path identities and canonical UUID text.
use super::gio;
use std::prelude::v1::*;
pub(super) fn identity(daemon: &str, path: &str) -> [u8; 16] {
    let hash = gio::checksum(&[daemon.as_bytes(), b"\0", path.as_bytes()]);
    hash[..16].try_into().unwrap()
}
pub(super) fn uuid(bytes: &[u8; 16]) -> String {
    let mut out = String::with_capacity(36);
    use std::fmt::Write;
    for (i, byte) in bytes.iter().enumerate() {
        if [4, 6, 8, 10].contains(&i) {
            out.push('-');
        }
        write!(out, "{byte:02x}").unwrap();
    }
    out
}
#[cfg(all(test, nxrs_native_witness))]
#[path = "../../../../../../tests/host/radio_shims/bluez_identity.rs"]
mod witness;
