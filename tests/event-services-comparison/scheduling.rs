//! Cooperative policy state, independent of the board and executor.
use core::{
    future::Future,
    pin::Pin,
    task::{Context, Poll},
};
pub const BUDGET_EVENTS: u32 = 4;
pub const BUDGET_US: u32 = 500;
pub const CHUNK_ITERATIONS: u32 = 10_000;

pub struct Budget {
    events: u32,
    started: u32,
}
impl Budget {
    pub fn new(now: u32) -> Self {
        Self {
            events: 0,
            started: now,
        }
    }
    pub fn reset(&mut self, now: u32) {
        self.events = 0;
        self.started = now;
    }
    pub fn handled(&mut self) {
        self.events += 1;
    }
    pub fn exhausted(&self, now: u32) -> bool {
        self.events >= BUDGET_EVENTS || now.wrapping_sub(self.started) >= BUDGET_US * 240
    }
}

/// One cooperative handoff, not a condition-polling or I/O-wait loop.
pub struct YieldOnce(bool);
impl Future for YieldOnce {
    type Output = ();
    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        if self.0 {
            Poll::Ready(())
        } else {
            self.0 = true;
            cx.waker().wake_by_ref();
            Poll::Pending
        }
    }
}
pub fn yield_once() -> YieldOnce {
    YieldOnce(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    use std::task::{Wake, Waker};
    struct CountWake(AtomicUsize);
    impl Wake for CountWake {
        fn wake(self: Arc<Self>) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
        fn wake_by_ref(self: &Arc<Self>) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }
    #[test]
    fn handoff_is_exactly_one_pending_poll_and_one_wake() {
        let counter = Arc::new(CountWake(AtomicUsize::new(0)));
        let waker = Waker::from(counter.clone());
        let mut cx = Context::from_waker(&waker);
        let mut handoff = yield_once();
        assert_eq!(Pin::new(&mut handoff).poll(&mut cx), Poll::Pending);
        assert_eq!(counter.0.load(Ordering::SeqCst), 1);
        assert_eq!(Pin::new(&mut handoff).poll(&mut cx), Poll::Ready(()));
        assert_eq!(counter.0.load(Ordering::SeqCst), 1);
    }
    #[test]
    fn budget_counts_events_and_reset() {
        let mut b = Budget::new(123);
        for _ in 0..3 {
            b.handled();
            assert!(!b.exhausted(123));
        }
        b.handled();
        assert!(b.exhausted(123));
        b.reset(999);
        assert!(!b.exhausted(999));
    }
    #[test]
    fn elapsed_budget_handles_counter_wrap() {
        let b = Budget::new(u32::MAX - 50);
        assert!(!b.exhausted(119_948));
        assert!(b.exhausted(119_949));
    }
}
