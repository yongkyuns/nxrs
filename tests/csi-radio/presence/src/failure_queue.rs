//! Other-peer copies made by the SDK preprocessing owner before queueing.
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
use std::sync::Mutex;
const SLOTS: usize = 10;
const WORDS: usize = 1024;
struct Matrix {
    cols: usize,
    rows0: usize,
    rows1: usize,
    data: [f32; WORDS],
}
impl Matrix {
    const ZERO: Self = Self {
        cols: 0,
        rows0: 0,
        rows1: 0,
        data: [0.0; WORDS],
    };
}
struct Slot {
    token: AtomicU32,
    value: Mutex<Matrix>,
}
impl Slot {
    const fn new() -> Self {
        Self {
            token: AtomicU32::new(0),
            value: Mutex::new(Matrix::ZERO),
        }
    }
}
pub struct Queue {
    enabled: AtomicBool,
    producer: AtomicUsize,
    consumer: AtomicUsize,
    next: AtomicU32,
    slots: [Slot; SLOTS],
    pub losses: AtomicU32,
}
impl Queue {
    pub const fn new() -> Self {
        Self {
            enabled: AtomicBool::new(false),
            producer: AtomicUsize::new(0),
            consumer: AtomicUsize::new(0),
            next: AtomicU32::new(0),
            slots: [const { Slot::new() }; SLOTS],
            losses: AtomicU32::new(0),
        }
    }
    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Acquire)
    }
    pub fn prepare(&self) -> bool {
        if self.enabled() || self.slots.iter().any(|s| s.value.try_lock().is_err()) {
            return false;
        }
        self.enabled.store(true, Ordering::Release);
        true
    }
    fn lost(&self) -> u32 {
        self.losses.fetch_add(1, Ordering::Relaxed);
        0
    }
    pub fn enqueue<'a>(
        &self,
        task: usize,
        cols: usize,
        begin: usize,
        count: usize,
        ring: usize,
        row: impl Fn(usize) -> Option<&'a [f32]>,
    ) -> u32 {
        if !self.enabled()
            || task == 0
            || cols == 0
            || cols > 64
            || ring == 0
            || ring > 1024
            || begin >= ring
            || count == 0
            || count > ring
            || count.checked_mul(cols).is_none_or(|n| n > WORDS)
        {
            return self.lost();
        }
        let owner = self.producer.load(Ordering::Acquire);
        if owner != task
            && !(owner == 0
                && self
                    .producer
                    .compare_exchange(0, task, Ordering::AcqRel, Ordering::Relaxed)
                    .is_ok())
        {
            return self.lost();
        }
        let Ok(previous) = self
            .next
            .fetch_update(Ordering::AcqRel, Ordering::Relaxed, |v| v.checked_add(1))
        else {
            return self.lost();
        };
        let token = previous + 1;
        let Some(slot) = self.slots.iter().find(|s| {
            s.token
                .compare_exchange(0, token, Ordering::AcqRel, Ordering::Relaxed)
                .is_ok()
        }) else {
            return self.lost();
        };
        let Ok(mut value) = slot.value.try_lock() else {
            slot.token.store(0, Ordering::Release);
            return self.lost();
        };
        value.cols = cols;
        value.rows0 = count.min(ring - begin);
        value.rows1 = count - value.rows0;
        for i in 0..count {
            let Some(r) = row((begin + i) % ring) else {
                slot.token.store(0, Ordering::Release);
                return self.lost();
            };
            if r.len() != cols {
                slot.token.store(0, Ordering::Release);
                return self.lost();
            }
            value.data[i * cols..(i + 1) * cols].copy_from_slice(r);
        }
        token
    }
    pub fn compute(
        &self,
        task: usize,
        token: u32,
        cols: usize,
        rows0: usize,
        rows1: usize,
        call: impl FnOnce(*const f32, *const f32) -> i32,
    ) -> i32 {
        let owner = self.consumer.load(Ordering::Acquire);
        if task == 0
            || (owner != task
                && !(owner == 0
                    && self
                        .consumer
                        .compare_exchange(0, task, Ordering::AcqRel, Ordering::Relaxed)
                        .is_ok()))
        {
            self.lost();
            return 0x103;
        }
        let Some(slot) = self
            .slots
            .iter()
            .find(|s| token != 0 && s.token.load(Ordering::Acquire) == token)
        else {
            self.lost();
            return 0x103;
        };
        let Ok(value) = slot.value.try_lock() else {
            self.lost();
            return 0x103;
        };
        if value.cols != cols || value.rows0 != rows0 || value.rows1 != rows1 {
            self.lost();
            drop(value);
            slot.token.store(0, Ordering::Release);
            return 0x103;
        }
        let result = call(value.data.as_ptr(), unsafe {
            value.data.as_ptr().add(rows0 * cols)
        });
        drop(value);
        slot.token.store(0, Ordering::Release);
        result
    }
}
pub static QUEUE: Queue = Queue::new();
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn enqueue_owned_inputs_survive_ring_reuse_and_native_failure_releases_slot() {
        let q = Queue::new();
        assert!(q.prepare());
        let mut rows = [[1., 2.], [3., 4.], [5., 6.]];
        let token = q.enqueue(1, 2, 2, 3, 3, |i| Some(&rows[i]));
        rows.fill([99., 99.]);
        assert_eq!(
            q.compute(9, token, 2, 1, 2, |a, b| {
                unsafe {
                    assert_eq!(std::slice::from_raw_parts(a, 2), &[5., 6.]);
                    assert_eq!(std::slice::from_raw_parts(b, 4), &[1., 2., 3., 4.]);
                }
                -1
            }),
            -1
        );
        assert!(q.slots.iter().all(|s| s.token.load(Ordering::Acquire) == 0));
    }
    #[test]
    fn queue_overload_missing_tokens_and_foreign_producers_do_not_substitute_inputs() {
        let q = Queue::new();
        assert!(q.prepare());
        let mut tokens = Vec::new();
        for _ in 0..SLOTS {
            tokens.push(q.enqueue(1, 2, 0, 1, 1, |_| Some(&[1., 2.])));
        }
        assert!(tokens.iter().all(|t| *t != 0));
        assert_eq!(q.enqueue(1, 2, 0, 1, 1, |_| Some(&[1., 2.])), 0);
        assert_eq!(
            q.compute(9, 0, 2, 1, 0, |_, _| panic!("no fallback")),
            0x103
        );
        assert_eq!(q.enqueue(2, 2, 0, 1, 1, |_| Some(&[1., 2.])), 0);
        assert_eq!(q.losses.load(Ordering::Acquire), 3);
    }
    #[test]
    fn wrong_split_and_paused_readers_fail_without_waiting() {
        let q = Queue::new();
        assert!(q.prepare());
        let token = q.enqueue(1, 2, 0, 1, 1, |_| Some(&[1., 2.]));
        let lock = q.slots[0].value.lock().unwrap();
        assert_eq!(
            q.compute(9, token, 2, 1, 0, |_, _| panic!("blocked reader")),
            0x103
        );
        drop(lock);
        assert_eq!(
            q.compute(9, token, 2, 0, 1, |_, _| panic!("wrong split")),
            0x103
        );
        assert_eq!(q.slots[0].token.load(Ordering::Acquire), 0);
        let token = q.enqueue(1, 2, 0, 1, 1, |_| Some(&[1., 2.]));
        assert_eq!(
            q.compute(8, token, 2, 1, 0, |_, _| panic!("foreign consumer")),
            0x103
        );
        assert_eq!(q.compute(9, token, 2, 1, 0, |_, _| 0), 0);
    }
}
