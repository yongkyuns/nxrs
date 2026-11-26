//! Retained replies for the navigation services' one-request control protocol.

use messaging::Sender;
use std::fmt;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::Mutex;

/// A command processed by the IMU or GNSS owner between measurement events.
/// Every successful command returns the resulting service status.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ControlRequest {
    Pause,
    Resume,
    Status,
}

/// Identity within one unique service handle, never reused by that handle.
///
/// Replies stay in the handle's private channel. A wake from an old service is
/// only a hint and cannot deliver an old reply to a replacement service handle.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RequestId(u64);

/// A request was not admitted; the caller still owns its retry/deadline policy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RequestError {
    /// Consume the preceding completion before admitting another request.
    Busy,
    /// The service inbox currently has no capacity.
    Full,
    /// The service no longer accepts requests.
    Closed,
    /// This handle has exhausted its request identifiers.
    Exhausted,
}

impl fmt::Display for RequestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Busy => "a navigation control request is still outstanding",
            Self::Full => "the navigation service inbox is full",
            Self::Closed => "the navigation service control channel is closed",
            Self::Exhausted => "the navigation service request identifiers are exhausted",
        })
    }
}

impl std::error::Error for RequestError {}

/// The owner retired its control channel without completing an admitted request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ControlFailure {
    /// Completion is no longer possible. This is not proof of joined teardown.
    Stopped,
}

impl fmt::Display for ControlFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the navigation service stopped before completing the request")
    }
}

impl std::error::Error for ControlFailure {}

/// An admitted request's retained result. Reading it releases its request slot.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ControlCompletion<S> {
    pub id: RequestId,
    pub request: ControlRequest,
    pub result: Result<S, ControlFailure>,
}

#[derive(Clone, Copy)]
pub(super) struct Ticket {
    pub id: RequestId,
    pub request: ControlRequest,
}

pub(super) struct Client<S> {
    // Preserve the handles' existing Send + Sync contract. All receiver access
    // requires &mut self and uses get_mut; no request/read acquires this mutex.
    replies: Mutex<Receiver<ControlCompletion<S>>>,
    pending: Option<Ticket>,
    next: Option<u64>,
}

pub(super) struct Replies<S, W: FnMut()> {
    sender: Option<SyncSender<ControlCompletion<S>>>,
    wake: Notification<W>,
}

/// Never re-enter a callback which panicked while signalling an earlier event.
/// The same guard protects retained control replies and fusion output wakes.
pub(super) struct Notification<W: FnMut()> {
    callback: W,
    available: bool,
}

impl<W: FnMut()> Notification<W> {
    pub fn new(callback: W) -> Self {
        Self {
            callback,
            available: true,
        }
    }

    pub fn notify(&mut self) {
        if self.available {
            self.available = false;
            (self.callback)();
            self.available = true;
        }
    }

    pub fn retire(&mut self) {
        if std::thread::panicking() {
            // An unrelated worker failure must still signal channel retirement.
            // A faulty callback cannot turn that existing unwind into an abort.
            let _ = catch_unwind(AssertUnwindSafe(|| self.notify()));
        } else {
            self.notify();
        }
    }
}

/// Reserve one reply slot before the worker starts. No request creates a channel.
pub(super) fn channel<S, W: FnMut()>(wake: W) -> (Client<S>, Replies<S, W>) {
    let (sender, replies) = sync_channel(1);
    (
        Client {
            replies: Mutex::new(replies),
            pending: None,
            next: Some(1),
        },
        Replies {
            sender: Some(sender),
            wake: Notification::new(wake),
        },
    )
}

impl<S> Client<S> {
    pub fn try_request<E>(
        &mut self,
        sender: &Sender<E>,
        request: ControlRequest,
        wrap: impl FnOnce(Ticket) -> E,
    ) -> Result<RequestId, RequestError> {
        if self.pending.is_some() {
            return Err(RequestError::Busy);
        }
        match self.receive() {
            Err(TryRecvError::Empty) => {}
            Err(TryRecvError::Disconnected) => return Err(RequestError::Closed),
            Ok(_) => panic!("navigation reply without an outstanding request"),
        }
        let id = self.next.ok_or(RequestError::Exhausted)?;
        let ticket = Ticket {
            id: RequestId(id),
            request,
        };
        sender.try_send(wrap(ticket)).map_err(|error| match error {
            TrySendError::Full(_) => RequestError::Full,
            TrySendError::Disconnected(_) => RequestError::Closed,
        })?;
        self.pending = Some(ticket);
        self.next = id.checked_add(1);
        Ok(ticket.id)
    }

    pub fn try_completion(&mut self) -> Option<ControlCompletion<S>> {
        let ticket = self.pending?;
        let completion = match self.receive() {
            Ok(completion) => {
                assert_eq!(completion.id, ticket.id, "reply belongs to another request");
                completion
            }
            Err(TryRecvError::Empty) => return None,
            Err(TryRecvError::Disconnected) => ControlCompletion {
                id: ticket.id,
                request: ticket.request,
                result: Err(ControlFailure::Stopped),
            },
        };
        self.pending = None;
        Some(completion)
    }

    fn receive(&mut self) -> Result<ControlCompletion<S>, TryRecvError> {
        self.replies
            .get_mut()
            .expect("reply receiver mutex is never locked")
            .try_recv()
    }
}

impl<S, W: FnMut()> Replies<S, W> {
    pub fn complete(&mut self, ticket: Ticket, status: S) {
        let completion = ControlCompletion {
            id: ticket.id,
            request: ticket.request,
            result: Ok(status),
        };
        match self.sender.as_ref().unwrap().try_send(completion) {
            Ok(()) => self.wake.notify(),
            // A dropped caller does not block or terminate the service.
            Err(TrySendError::Disconnected(_)) => {}
            // One admitted request stays pending until its retained reply is
            // consumed. Filling this slot twice is an internal protocol bug.
            Err(TrySendError::Full(_)) => panic!("navigation reply slot already occupied"),
        }
    }
}

impl<S, W: FnMut()> Drop for Replies<S, W> {
    fn drop(&mut self) {
        // Retire the channel before notifying, so a caller that wakes immediately
        // observes closure. Buffered completions remain readable after closure.
        drop(self.sender.take());
        self.wake.retire();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc;
    use std::sync::Arc;

    #[test]
    fn existing_handles_remain_send_and_sync() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<crate::ImuHandle>();
        assert_send_sync::<crate::GnssHandle>();
    }

    #[test]
    fn panicked_completion_wake_is_not_reentered_during_retirement() {
        let calls = Arc::new(AtomicUsize::new(0));
        let count = calls.clone();
        let (mut client, mut replies) = channel(move || {
            count.fetch_add(1, Ordering::SeqCst);
            panic!("wake failed");
        });
        let (sender, inbox) = messaging::bounded(1);
        let id = client
            .try_request(&sender, ControlRequest::Status, |ticket| ticket)
            .unwrap();
        let worker = std::thread::spawn(move || {
            replies.complete(inbox.try_recv().unwrap(), 7);
        });
        assert!(worker.join().is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let completion = client.try_completion().unwrap();
        assert_eq!(completion.id, id);
        assert_eq!(completion.result, Ok(7));
        assert_eq!(
            client.try_request(&sender, ControlRequest::Status, |ticket| ticket),
            Err(RequestError::Closed)
        );
    }

    #[test]
    fn unrelated_worker_unwind_still_notifies_without_a_double_panic() {
        for faulty_wake in [false, true] {
            let calls = Arc::new(AtomicUsize::new(0));
            let count = calls.clone();
            let (mut client, replies) = channel::<(), _>(move || {
                count.fetch_add(1, Ordering::SeqCst);
                assert!(!faulty_wake, "retirement wake failed");
            });
            let (sender, _inbox) = messaging::bounded(1);
            let id = client
                .try_request(&sender, ControlRequest::Status, |ticket| ticket)
                .unwrap();
            let worker = std::thread::spawn(move || {
                let _replies = replies;
                panic!("worker failed");
            });
            let failure = worker.join().unwrap_err();
            assert_eq!(failure.downcast_ref::<&str>(), Some(&"worker failed"));
            assert_eq!(calls.load(Ordering::SeqCst), 1);
            assert_eq!(
                client.try_completion(),
                Some(ControlCompletion {
                    id,
                    request: ControlRequest::Status,
                    result: Err(ControlFailure::Stopped),
                })
            );
        }
    }

    #[test]
    fn delayed_owner_and_expired_caller_keep_one_request_pending() {
        let (sender, inbox) = messaging::bounded(2);
        let (mut client, mut replies) = channel(|| {});
        let id = client
            .try_request(&sender, ControlRequest::Pause, |ticket| ticket)
            .unwrap();
        // The owner has not dispatched. The caller can handle unrelated work
        // and expire its own deadline, but expiry cannot release this slot.
        assert_eq!(client.try_completion(), None);
        for request in [ControlRequest::Resume, ControlRequest::Status] {
            assert_eq!(
                client.try_request(&sender, request, |ticket| ticket),
                Err(RequestError::Busy)
            );
        }
        replies.complete(inbox.try_recv().unwrap(), false);
        // Even an already completed request remains pending until consumed.
        assert_eq!(
            client.try_request(&sender, ControlRequest::Resume, |ticket| ticket),
            Err(RequestError::Busy)
        );
        assert_eq!(
            client.try_completion(),
            Some(ControlCompletion {
                id,
                request: ControlRequest::Pause,
                result: Ok(false),
            })
        );
        let next = client
            .try_request(&sender, ControlRequest::Resume, |ticket| ticket)
            .unwrap();
        assert_ne!(next, id);
    }

    #[test]
    fn full_wake_inbox_cannot_discard_the_completion() {
        let (wake, notifications) = mpsc::sync_channel(1);
        wake.try_send(()).unwrap();
        let (mut client, mut replies) = channel(move || {
            let _ = wake.try_send(());
        });
        let (sender, inbox) = messaging::bounded(1);
        let id = client
            .try_request(&sender, ControlRequest::Status, |ticket| ticket)
            .unwrap();
        replies.complete(inbox.try_recv().unwrap(), 42);
        notifications.try_recv().unwrap(); // The unrelated preexisting wake.
        assert_eq!(notifications.try_recv(), Err(TryRecvError::Empty));
        let completion = client.try_completion().unwrap();
        assert_eq!(completion.id, id);
        assert_eq!(completion.result, Ok(42));
        assert_eq!(client.try_completion(), None);
    }

    #[test]
    fn rejected_admission_never_occupies_the_reply_slot() {
        let (sender, inbox) = messaging::bounded(1);
        let (mut client, _replies) = channel::<(), _>(|| {});
        sender
            .try_send(Ticket {
                id: RequestId(0),
                request: ControlRequest::Status,
            })
            .ok()
            .unwrap();
        assert_eq!(
            client.try_request(&sender, ControlRequest::Pause, |ticket| ticket),
            Err(RequestError::Full)
        );
        inbox.try_recv().unwrap();
        assert!(client
            .try_request(&sender, ControlRequest::Pause, |ticket| ticket)
            .is_ok());
    }

    #[test]
    fn owner_retirement_preserves_success_or_reports_pending_failure() {
        for completed in [false, true] {
            let (sender, inbox) = messaging::bounded(1);
            let (wake, notifications) = mpsc::channel();
            let (mut client, mut replies) = channel(move || {
                wake.send(()).unwrap();
            });
            let id = client
                .try_request(&sender, ControlRequest::Status, |ticket| ticket)
                .unwrap();
            if completed {
                replies.complete(inbox.try_recv().unwrap(), 7);
                notifications.try_recv().unwrap();
            }
            drop(inbox);
            drop(replies);
            notifications.try_recv().unwrap();
            assert_eq!(
                client.try_completion(),
                Some(ControlCompletion {
                    id,
                    request: ControlRequest::Status,
                    result: if completed {
                        Ok(7)
                    } else {
                        Err(ControlFailure::Stopped)
                    },
                })
            );
            assert_eq!(
                client.try_request(&sender, ControlRequest::Status, |ticket| ticket),
                Err(RequestError::Closed)
            );
        }
    }

    #[test]
    fn dropping_the_reader_does_not_wait_for_a_reply() {
        let (sender, inbox) = messaging::bounded(1);
        let (mut client, mut replies) = channel(|| {});
        client
            .try_request(&sender, ControlRequest::Pause, |ticket| ticket)
            .unwrap();
        drop(client);
        replies.complete(inbox.try_recv().unwrap(), false);
    }

    #[test]
    fn an_old_session_wake_cannot_complete_a_replacement_request() {
        let (wake, notifications) = mpsc::channel();
        let old_wake = wake.clone();
        let (mut old, mut old_replies) = channel(move || {
            let _ = old_wake.send(());
        });
        let (old_sender, old_inbox) = messaging::bounded(1);
        let old_id = old
            .try_request(&old_sender, ControlRequest::Status, |ticket| ticket)
            .unwrap();
        drop(old);
        let (mut current, mut current_replies) = channel(move || {
            let _ = wake.send(());
        });
        let (sender, inbox) = messaging::bounded(1);
        let id = current
            .try_request(&sender, ControlRequest::Status, |ticket| ticket)
            .unwrap();
        assert_eq!(id, old_id); // Identifiers are intentionally handle-scoped.
        old_replies.complete(old_inbox.try_recv().unwrap(), 1);
        drop(old_replies); // A delayed old-session retirement wake.
        notifications.try_recv().unwrap();
        assert_eq!(current.try_completion(), None);
        current_replies.complete(inbox.try_recv().unwrap(), 2);
        assert_eq!(current.try_completion().unwrap().result, Ok(2));
    }

    #[test]
    fn retired_control_rejects_requests_before_provider_cleanup_finishes() {
        let (sender, _still_live_inbox) = messaging::bounded(1);
        let (mut client, replies) = channel::<(), _>(|| {});
        drop(replies);
        assert_eq!(
            client.try_request(&sender, ControlRequest::Status, |ticket| ticket),
            Err(RequestError::Closed)
        );
    }

    #[test]
    fn identifiers_never_wrap_or_reuse_on_a_handle() {
        let (sender, inbox) = messaging::bounded(1);
        let (mut client, mut replies) = channel(|| {});
        client.next = Some(u64::MAX);
        assert_eq!(
            client.try_request(&sender, ControlRequest::Status, |ticket| ticket),
            Ok(RequestId(u64::MAX))
        );
        replies.complete(inbox.try_recv().unwrap(), ());
        client.try_completion().unwrap();
        assert_eq!(
            client.try_request(&sender, ControlRequest::Status, |ticket| ticket),
            Err(RequestError::Exhausted)
        );
    }
}
