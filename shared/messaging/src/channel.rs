//! Minimal bounded event transport for active Nxrs entities.
//!
//! This is intentionally a thin ownership wrapper around
//! `std::sync::mpsc::sync_channel`: many producers clone a `Sender<E>`,
//! exactly one owner holds the `Inbox<E>`, and that owner has one blocking
//! wait point.
#![forbid(unsafe_code)]

use std::sync::mpsc::{
    sync_channel, Receiver, RecvTimeoutError, SendError, SyncSender, TryRecvError, TrySendError,
};
use std::time::Duration;

/// Create one bounded MPSC inbox and its first sender.
///
/// Capacity is explicit because an active service must have a finite event
/// backlog. Clone the returned sender to connect additional producers to the
/// same owner inbox.
pub fn bounded<E>(capacity: usize) -> (Sender<E>, Inbox<E>) {
    assert!(capacity > 0, "event inbox capacity must be nonzero");
    let (sender, receiver) = sync_channel(capacity);
    (Sender { sender }, Inbox { receiver })
}

/// Producer endpoint for one active entity's private event type.
pub struct Sender<E> {
    sender: SyncSender<E>,
}

impl<E> Clone for Sender<E> {
    fn clone(&self) -> Self {
        Self {
            sender: self.sender.clone(),
        }
    }
}

impl<E> Sender<E> {
    /// Reliable/blocking admission. Use only where waiting for capacity is
    /// explicitly acceptable (for example top-level lifecycle/control code).
    pub fn send(&self, event: E) -> Result<(), SendError<E>> {
        self.sender.send(event)
    }

    /// Non-blocking admission for run-to-completion producers.
    pub fn try_send(&self, event: E) -> Result<(), TrySendError<E>> {
        self.sender.try_send(event)
    }
}

/// Unique consumer endpoint owned by the active entity.
///
/// `Inbox` is intentionally not Clone: one execution owner serializes all
/// mutations of the entity's private state.
pub struct Inbox<E> {
    receiver: Receiver<E>,
}

impl<E> Inbox<E> {
    /// Canonical active-entity wait point.
    ///
    /// `None` waits only for an event. `Some(timeout)` waits for either an
    /// event or deadline expiry. Both modes use the same owner inbox and map
    /// disconnect to `RecvTimeoutError::Disconnected`.
    pub fn wait(&self, timeout: Option<Duration>) -> Result<E, RecvTimeoutError> {
        match timeout {
            Some(timeout) => self.receiver.recv_timeout(timeout),
            None => self
                .receiver
                .recv()
                .map_err(|_| RecvTimeoutError::Disconnected),
        }
    }

    /// Non-blocking receive for explicit drain/inspection paths.
    pub fn try_recv(&self) -> Result<E, TryRecvError> {
        self.receiver.try_recv()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cloned_senders_fan_in_to_one_inbox() {
        let (a, inbox) = bounded(4);
        let b = a.clone();

        a.send(("a", 1)).unwrap();
        b.send(("b", 1)).unwrap();
        a.send(("a", 2)).unwrap();

        assert_eq!(inbox.wait(None).unwrap(), ("a", 1));
        assert_eq!(inbox.wait(None).unwrap(), ("b", 1));
        assert_eq!(inbox.wait(None).unwrap(), ("a", 2));
    }

    #[test]
    fn bounded_try_send_reports_overload_without_blocking() {
        let (sender, inbox) = bounded(1);
        sender.try_send(1).unwrap();
        assert!(matches!(sender.try_send(2), Err(TrySendError::Full(2))));
        assert_eq!(inbox.wait(None).unwrap(), 1);
    }

    #[test]
    fn timeout_uses_the_same_inbox_wait_point() {
        let (_sender, inbox) = bounded::<u8>(1);
        assert_eq!(
            inbox.wait(Some(Duration::from_millis(1))),
            Err(RecvTimeoutError::Timeout)
        );
    }

    #[test]
    fn wait_without_deadline_uses_the_same_inbox() {
        let (sender, inbox) = bounded(1);
        std::thread::spawn(move || sender.send(7u8).unwrap());
        assert_eq!(inbox.wait(None), Ok(7));
    }

    #[test]
    fn each_concurrent_producer_preserves_its_own_order() {
        let (first, inbox) = bounded(16);
        let second = first.clone();

        let a = std::thread::spawn(move || {
            for sequence in 0..4 {
                first.send(('a', sequence)).unwrap();
            }
        });
        let b = std::thread::spawn(move || {
            for sequence in 0..4 {
                second.send(('b', sequence)).unwrap();
            }
        });

        let mut next_a = 0;
        let mut next_b = 0;
        for _ in 0..8 {
            let (producer, sequence) = inbox.wait(None).unwrap();
            match producer {
                'a' => {
                    assert_eq!(sequence, next_a);
                    next_a += 1;
                }
                'b' => {
                    assert_eq!(sequence, next_b);
                    next_b += 1;
                }
                _ => unreachable!(),
            }
        }

        a.join().unwrap();
        b.join().unwrap();
        assert_eq!((next_a, next_b), (4, 4));
    }

    #[test]
    fn disconnect_is_explicit() {
        let (sender, inbox) = bounded::<u8>(1);
        drop(sender);
        assert!(inbox.wait(None).is_err());
    }
}
