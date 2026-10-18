//! Fixed-capacity mapping from scan-local peer IDs to opaque native identities.
use crate::{Error, PeerId, SessionId};
use std::sync::{Mutex, TryLockError};

const CAPACITY: usize = 32;

#[derive(Default)]
struct Table {
    session: Option<SessionId>,
    entries: [Option<[u8; 16]>; CAPACITY],
}

impl Table {
    fn bind(&mut self, session: SessionId) -> Result<(), Error> {
        // The shared scan lease admits one owner, not 32 concurrent sessions.
        if self.session.is_some() {
            return Err(Error::Busy);
        }
        self.session = Some(session);
        Ok(())
    }

    fn remember(&mut self, session: SessionId, id: PeerId, peer: [u8; 16]) -> Result<(), Error> {
        if id.0 == 0 || id.0 > CAPACITY as u64 || peer == [0; 16] {
            return Err(Error::InvalidValue);
        }
        if self.session != Some(session) {
            return Err(Error::Unavailable);
        }
        let index = id.0 as usize - 1;
        if self.entries[index].is_some_and(|previous| previous != peer)
            || self
                .entries
                .iter()
                .enumerate()
                .any(|(other, entry)| other != index && *entry == Some(peer))
        {
            return Err(Error::InvalidValue);
        }
        self.entries[index] = Some(peer);
        Ok(())
    }

    fn resolve(&self, session: SessionId, id: PeerId) -> Result<[u8; 16], Error> {
        if id.0 == 0 || id.0 > CAPACITY as u64 {
            return Err(Error::InvalidValue);
        }
        if self.session != Some(session) {
            return Err(Error::Unavailable);
        }
        self.entries[id.0 as usize - 1].ok_or(Error::Unavailable)
    }

    fn expire(&mut self, session: SessionId) {
        if self.session == Some(session) {
            *self = Self::default();
        }
    }
}

fn table() -> &'static Mutex<Table> {
    // No first-use OnceLock wait on the GATT consumer's try-lock path.
    static TABLE: Mutex<Table> = Mutex::new(Table {
        session: None,
        entries: [None; CAPACITY],
    });
    &TABLE
}

pub(super) fn bind(session: SessionId) -> Result<(), Error> {
    table().lock().map_err(|_| Error::Io)?.bind(session)
}

pub(super) fn remember(session: SessionId, id: PeerId, peer: [u8; 16]) -> Result<(), Error> {
    table()
        .lock()
        .map_err(|_| Error::Io)?
        .remember(session, id, peer)
}

pub(super) fn expire(session: SessionId) {
    if let Ok(mut table) = table().lock() {
        table.expire(session);
    }
}

fn resolve_locked(lock: &Mutex<Table>, peer: crate::gatt::Peer) -> Result<[u8; 16], Error> {
    match lock.try_lock() {
        Ok(table) => table.resolve(peer.session, peer.id),
        Err(TryLockError::WouldBlock) => Err(Error::Busy),
        Err(TryLockError::Poisoned(_)) => Err(Error::Io),
    }
}

#[cfg(all(
    nxrs_provider = "native",
    any(target_os = "macos", target_os = "linux")
))]
pub(super) fn resolve(peer: crate::gatt::Peer) -> Result<[u8; 16], Error> {
    resolve_locked(table(), peer)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session(value: u64) -> SessionId {
        SessionId::new(value).unwrap()
    }

    #[test]
    fn mapping_is_stable_bounded_and_never_overwritten() {
        let mut table = Table::default();
        let owner = session(1);
        table.bind(owner).unwrap();
        for index in 1..=CAPACITY {
            let identity = [index as u8; 16];
            table
                .remember(owner, PeerId(index as u64), identity)
                .unwrap();
            assert_eq!(table.resolve(owner, PeerId(index as u64)), Ok(identity));
            assert_eq!(
                table.remember(owner, PeerId(index as u64), identity),
                Ok(())
            );
        }
        for (id, identity) in [
            (PeerId(1), [99; 16]),
            (PeerId(33), [33; 16]),
            (PeerId(0), [1; 16]),
            (PeerId(2), [0; 16]),
        ] {
            assert_eq!(
                table.remember(owner, id, identity),
                Err(Error::InvalidValue)
            );
        }
        assert_eq!(table.resolve(owner, PeerId(1)), Ok([1; 16]));
        assert_eq!(table.bind(owner), Err(Error::Busy));
        let other = session(2);
        assert_eq!(table.bind(other), Err(Error::Busy));
        assert_eq!(
            table.remember(other, PeerId(1), [33; 16]),
            Err(Error::Unavailable)
        );
    }

    #[test]
    fn expiry_removes_only_the_selected_session_and_rejects_stale_ids() {
        let mut table = Table::default();
        let first = session(10);
        let second = session(11);
        table.bind(first).unwrap();
        table.remember(first, PeerId(1), [1; 16]).unwrap();
        assert_eq!(
            table.remember(first, PeerId(2), [1; 16]),
            Err(Error::InvalidValue)
        );
        table.expire(second);
        assert_eq!(table.resolve(first, PeerId(1)), Ok([1; 16]));
        table.expire(first);
        assert_eq!(table.resolve(first, PeerId(1)), Err(Error::Unavailable));
        table.bind(second).unwrap();
        table.remember(second, PeerId(1), [2; 16]).unwrap();
        table.expire(first);
        assert_eq!(table.resolve(second, PeerId(1)), Ok([2; 16]));
    }

    #[test]
    fn resolver_uses_nonblocking_lock_admission() {
        let session = session(20);
        assert_eq!(
            resolve_locked(
                &Mutex::new(Table::default()),
                crate::gatt::Peer {
                    session,
                    id: PeerId(1),
                }
            ),
            Err(Error::Unavailable)
        );
        let lock = Mutex::new(Table::default());
        let _guard = lock.lock().unwrap();
        assert_eq!(
            resolve_locked(
                &lock,
                crate::gatt::Peer {
                    session,
                    id: PeerId(1),
                }
            ),
            Err(Error::Busy)
        );
    }
}
