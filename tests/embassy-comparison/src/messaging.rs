//! Multi-queue waiting shared by firmware and wakeup regression tests.
use core::{future::poll_fn, task::Poll};
use embassy_sync::{blocking_mutex::raw::CriticalSectionRawMutex, channel::Channel};

/// Round-robin selection with one receiving owner per queue. Every empty
/// channel registers the same task waker, so any eligible queue wakes it.
pub async fn receive_any<T, const N: usize>(
    queues: &[Channel<CriticalSectionRawMutex, T, N>],
    cursor: &mut usize,
) -> (usize, T) {
    assert!(!queues.is_empty());
    poll_fn(|cx| {
        for offset in 0..queues.len() {
            let index = (*cursor + offset) % queues.len();
            if let Poll::Ready(value) = queues[index].poll_receive(cx) {
                *cursor = (index + 1) % queues.len();
                return Poll::Ready((index, value));
            }
        }
        Poll::Pending
    })
    .await
}
