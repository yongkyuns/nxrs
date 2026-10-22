//! Process-lifetime SDK handoff. Only the callback touches the producer while active.
//! The consumer has a separate lock; preempting it cannot block producer progress.
use crate::{Frame, Metadata, Observation, Queue, QUEUE_SIZE};
use heapless::spsc::{Consumer, Producer};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex,
};

const SLOTS: usize = QUEUE_SIZE + 1; // heapless reserves one slot.
struct Writer {
    admission: Queue,
    ring: Producer<'static, Frame, SLOTS>,
}
pub struct Handoff {
    writer: Mutex<Writer>,
    reader: Mutex<Consumer<'static, Frame, SLOTS>>,
    running: AtomicBool,
    failed: AtomicBool,
}
impl Default for Handoff {
    fn default() -> Self {
        Self::new()
    }
}
impl Handoff {
    /// Allocate once before SDK admission. Storage intentionally survives failed
    /// teardown and late callbacks; native wiring keeps one static instance.
    pub fn new() -> Self {
        let queue = Box::leak(Box::new(heapless::spsc::Queue::new()));
        let (ring, reader) = queue.split();
        Self {
            writer: Mutex::new(Writer {
                admission: Queue::new(),
                ring,
            }),
            reader: Mutex::new(reader),
            running: AtomicBool::new(false),
            failed: AtomicBool::new(false),
        }
    }
    /// Called only after previous SDK retirement and in-flight callback barrier.
    pub fn begin(&self, epoch: u32, peer: [u8; 6]) -> Result<(), &'static str> {
        if self.running.load(Ordering::Acquire) {
            return Err("already active");
        }
        let mut writer = self.writer.lock().map_err(|_| "writer poisoned")?;
        let mut reader = self.reader.lock().map_err(|_| "reader poisoned")?;
        writer.admission.begin(epoch, peer)?;
        for _ in 0..QUEUE_SIZE {
            if reader.dequeue().is_none() {
                break;
            }
        }
        self.failed.store(false, Ordering::Relaxed);
        self.running.store(true, Ordering::Release);
        Ok(())
    }
    /// Native callback only. Bounded try-lock, one bounded copy, one SPSC enqueue;
    /// no allocator, wait, retry loop, consumer lock or SDK call.
    pub fn push(&self, epoch: u32, metadata: Metadata, bytes: &[i8]) -> bool {
        let Ok(mut writer) = self.writer.try_lock() else {
            return false;
        };
        if let Some(frame) = writer.admission.admit(epoch, metadata, bytes) {
            if writer.ring.enqueue(frame).is_err() {
                writer.admission.overflow += 1;
            }
        }
        if writer.admission.overflow != 0 || writer.admission.malformed != 0 {
            self.failed.store(true, Ordering::Release);
        }
        true
    }
    pub fn pop(&self) -> Result<Option<Frame>, &'static str> {
        Ok(self.reader.lock().map_err(|_| "reader poisoned")?.dequeue())
    }
    pub fn failed(&self) -> bool {
        self.failed.load(Ordering::Acquire)
    }
    /// Caller has closed native admission, unregistered/disabled SDK callbacks,
    /// and passed the in-flight barrier. Future old-epoch callbacks are rejected.
    pub fn retire(&self) -> Result<(), &'static str> {
        self.writer
            .lock()
            .map_err(|_| "writer poisoned")?
            .admission
            .end();
        self.running.store(false, Ordering::Release);
        Ok(())
    }
    /// No main-thread producer locking is permitted during active capture.
    pub fn snapshot(&self) -> Result<(Observation, u64, u64), &'static str> {
        if self.running.load(Ordering::Acquire) {
            return Err("capture active");
        }
        let writer = self.writer.lock().map_err(|_| "writer poisoned")?;
        let q = &writer.admission;
        Ok((q.observation, q.overflow, q.malformed))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn metadata() -> Metadata {
        super::super::tests::metadata()
    }
    #[test]
    fn preempted_consumer_cannot_block_callback() {
        let h = Handoff::new();
        let m = metadata();
        h.begin(1, m.mac).unwrap();
        let paused_consumer = h.reader.lock().unwrap();
        assert!(h.push(1, m, &[7; 128]));
        assert!(!h.failed());
        assert!(h.snapshot().is_err());
        drop(paused_consumer);
        let paused_producer = h.writer.lock().unwrap();
        assert_eq!(h.pop().unwrap().unwrap().data[0], 7);
        drop(paused_producer);
    }
    #[test]
    fn overflow_is_sticky_and_keeps_all_committed_frames() {
        let h = Handoff::new();
        let m = metadata();
        h.begin(1, m.mac).unwrap();
        for n in 0..QUEUE_SIZE + 1 {
            assert!(h.push(1, m, &[n as i8; 128]));
        }
        assert!(h.failed());
        for n in 0..QUEUE_SIZE {
            assert_eq!(h.pop().unwrap().unwrap().data[0], n as i8);
        }
        assert!(h.pop().unwrap().is_none());
        assert!(h.failed());
        h.retire().unwrap();
        assert_eq!(h.snapshot().unwrap().1, 1);
    }
    #[test]
    fn retire_and_reacquire_discard_old_frames_and_epochs() {
        let h = Handoff::new();
        let m = metadata();
        h.begin(1, m.mac).unwrap();
        assert!(h.begin(2, m.mac).is_err());
        assert!(h.push(1, m, &[1; 128]));
        h.retire().unwrap();
        assert!(h.push(1, m, &[2; 128]));
        h.begin(2, m.mac).unwrap();
        assert!(h.push(1, m, &[3; 128]));
        assert!(h.pop().unwrap().is_none());
        assert!(h.push(2, m, &[4; 128]));
        assert_eq!(h.pop().unwrap().unwrap().data[0], 4);
        h.retire().unwrap();
        assert_eq!(h.snapshot().unwrap().0.callbacks, 1);
    }
    #[test]
    fn callback_contention_rejects_without_wait_and_malformed_is_sticky() {
        let h = Handoff::new();
        let m = metadata();
        h.begin(1, m.mac).unwrap();
        let paused_callback = h.writer.lock().unwrap();
        assert!(!h.push(1, m, &[0; 128]));
        drop(paused_callback);
        assert!(h.push(1, m, &[]));
        assert!(h.failed());
        assert!(h.pop().unwrap().is_none());
    }
}
