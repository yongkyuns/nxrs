//! Bounded peer ownership and scan/GATT exclusion, implemented in Rust.
#![allow(unsafe_code)]
use std::sync::{Mutex, TryLockError};

#[derive(Clone, Copy, Default, PartialEq, Eq, Debug)]
pub(super) struct Peer {
    pub address: [u8; 6],
    pub address_type: u8,
}

struct Table {
    fd: Option<i32>,
    session: u64,
    scanning: bool,
    peers: [Option<Peer>; 32],
}
impl Table {
    const fn new() -> Self {
        Self {
            fd: None,
            session: 0,
            scanning: false,
            peers: [None; 32],
        }
    }
    fn bind(&mut self, fd: i32, session: u64) -> Result<(), u32> {
        if fd < 0 || session == 0 {
            return Err(1);
        }
        if self.fd.is_some_and(|owner| owner != fd) {
            return Err(3);
        }
        if self.fd != Some(fd) || self.session != session {
            self.peers = [None; 32];
            self.fd = Some(fd);
            self.session = session;
        }
        Ok(())
    }
    fn remember(&mut self, fd: i32, session: u64, id: u64, peer: Peer) -> Result<(), u32> {
        let slot = id.checked_sub(1).filter(|&i| i < 32).ok_or(1u32)? as usize;
        if self.fd != Some(fd) || self.session != session {
            return Err(2);
        }
        if self.peers[slot].is_some_and(|old| old != peer) {
            return Err(1);
        }
        self.peers[slot] = Some(peer);
        Ok(())
    }
    fn resolve(&self, session: u64, id: u64) -> Result<Peer, u32> {
        let slot = id.checked_sub(1).filter(|&i| i < 32).ok_or(1u32)? as usize;
        if self.fd.is_none() || self.session != session {
            return Err(2);
        }
        let peer = self.peers[slot].ok_or(2u32)?;
        if self.scanning {
            Err(3)
        } else {
            Ok(peer)
        }
    }
}
static TABLE: Mutex<Table> = Mutex::new(Table::new());

fn with<T>(lock: &Mutex<Table>, f: impl FnOnce(&mut Table) -> Result<T, u32>) -> Result<T, u32> {
    match lock.try_lock() {
        Ok(mut table) => f(&mut table),
        Err(TryLockError::WouldBlock) => Err(3),
        Err(TryLockError::Poisoned(_)) => Err(4),
    }
}

pub(super) fn bind(fd: i32, session: u64) -> Result<(), crate::Error> {
    with(&TABLE, |t| t.bind(fd, session)).map_err(error)
}
pub(super) fn remember(fd: i32, session: u64, id: u64, peer: Peer) -> Result<(), crate::Error> {
    with(&TABLE, |t| t.remember(fd, session, id, peer)).map_err(error)
}
pub(super) fn scanning(fd: i32, scanning: bool) -> Result<(), crate::Error> {
    with(&TABLE, |t| {
        if t.fd == Some(fd) {
            t.scanning = scanning;
        }
        Ok(())
    })
    .map_err(error)
}
pub(super) fn expire(fd: i32) {
    // Only the scanner owns mutation. A short GATT lookup cannot retain this lock.
    if let Ok(mut t) = TABLE.lock() {
        if t.fd == Some(fd) {
            *t = Table::new();
        }
    }
}
fn error(code: u32) -> crate::Error {
    match code {
        1 => crate::Error::InvalidValue,
        2 => crate::Error::Unavailable,
        3 => crate::Error::Busy,
        _ => crate::Error::Io,
    }
}

#[cfg(not(test))]
#[no_mangle]
unsafe extern "C" fn nxrs_ble_peer_lookup_rust(
    session: u64,
    id: u64,
    address: *mut u8,
    kind: *mut u8,
) -> u32 {
    if address.is_null() || kind.is_null() {
        return 1;
    }
    match with(&TABLE, |t| t.resolve(session, id)) {
        Ok(peer) => {
            // SAFETY: C's private wrapper supplies writable six-byte/type outputs.
            unsafe {
                core::ptr::copy_nonoverlapping(peer.address.as_ptr(), address, 6);
                *kind = peer.address_type;
            }
            0
        }
        Err(code) => code,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identities_cannot_be_overwritten_and_scanning_excludes_gatt_until_confirmed_stop() {
        let mut t = Table::new();
        assert_eq!(t.bind(7, 0), Err(1));
        t.bind(7, 44).unwrap();
        assert_eq!(t.bind(8, 44), Err(3));
        let peer = Peer {
            address: [42, 1, 2, 3, 4, 5],
            address_type: 2,
        };
        t.remember(7, 44, 1, peer).unwrap();
        t.remember(7, 44, 1, peer).unwrap();
        assert_eq!(t.remember(7, 44, 1, Peer::default()), Err(1));
        for id in [0, 33] {
            assert_eq!(t.remember(7, 44, id, peer), Err(1));
        }
        assert_eq!(t.resolve(45, 1), Err(2));
        t.scanning = true;
        assert_eq!(t.resolve(44, 1), Err(3));
        // A failed native stop leaves this state intact; only successful stop clears it.
        assert_eq!(t.resolve(44, 1), Err(3));
        t.scanning = false;
        assert_eq!(t.resolve(44, 1), Ok(peer));
        t.bind(7, 45).unwrap();
        assert_eq!(t.resolve(44, 1), Err(2));
        assert_eq!(t.resolve(45, 1), Err(2));
    }
    #[test]
    fn callback_lookup_never_waits_on_a_registry_lock() {
        let lock = Mutex::new(Table::new());
        let _guard = lock.lock().unwrap();
        assert_eq!(with(&lock, |t| t.resolve(1, 1)), Err(3));
    }
}
