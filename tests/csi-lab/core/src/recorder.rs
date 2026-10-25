//! Callback-safe bounded transport: allocate on prepare, try-lock on producer,
//! format/write only on the owner task. Any overflow/contention is permanent loss.
use crate::journal::{self, Record, END};
use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Mutex;
struct Queue {
    slots: Box<[Record]>,
    head: usize,
    len: usize,
    next: u32,
    chain: u32,
}
pub struct Recorder {
    enabled: AtomicBool,
    accepting: AtomicBool,
    lost: AtomicU32,
    active: AtomicU32,
    queue: Mutex<Option<Queue>>,
}
impl Default for Recorder {
    fn default() -> Self {
        Self::new()
    }
}
impl Recorder {
    pub const fn new() -> Self {
        Self {
            enabled: AtomicBool::new(false),
            accepting: AtomicBool::new(false),
            lost: AtomicU32::new(0),
            active: AtomicU32::new(0),
            queue: Mutex::new(None),
        }
    }
    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Acquire)
    }
    pub fn prepare(&self, config: crate::calibration::Config, capacity: usize) -> bool {
        if capacity == 0 || capacity > 256 {
            return false;
        }
        let Ok(mut q) = self.queue.lock() else {
            return false;
        };
        if q.is_some() {
            return false;
        }
        let r = journal::session(config);
        let mut slots = vec![Record::EMPTY; capacity].into_boxed_slice();
        slots[0] = r;
        *q = Some(Queue {
            slots,
            head: 0,
            len: 1,
            next: 1,
            chain: journal::chain(0, r.checksum()),
        });
        self.enabled.store(true, Ordering::Release);
        self.accepting.store(true, Ordering::SeqCst);
        true
    }
    /// Holds recording admission across a synchronous native call. finish()
    /// waits for these leases before writing END; late callbacks cannot join.
    pub fn lease(&self) -> Option<Lease<'_>> {
        // Admission and in-flight count need one total order: close must not
        // observe zero while an entrant still observes an open gate.
        if !self.accepting.load(Ordering::SeqCst) {
            return None;
        }
        self.active.fetch_add(1, Ordering::SeqCst);
        if self.accepting.load(Ordering::SeqCst) {
            Some(Lease(self))
        } else {
            self.active.fetch_sub(1, Ordering::SeqCst);
            None
        }
    }
    pub fn push(&self, r: Record) {
        if let Some(lease) = self.lease() {
            lease.push(r);
        }
    }
    fn push_admitted(&self, mut r: Record) {
        match self.queue.try_lock() {
            Ok(mut q) => {
                if let Some(q) = q.as_mut() {
                    if q.len == q.slots.len() || q.next == u32::MAX {
                        self.lost.fetch_add(1, Ordering::Relaxed);
                    } else {
                        r.sequence = q.next;
                        q.next += 1;
                        q.chain = journal::chain(q.chain, r.checksum());
                        let i = (q.head + q.len) % q.slots.len();
                        q.slots[i] = r;
                        q.len += 1;
                    }
                }
            }
            Err(_) => {
                self.lost.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
    pub fn loss(&self) -> u32 {
        self.lost.load(Ordering::Acquire)
    }
    pub fn invalidate(&self) {
        if self.enabled() {
            self.lost.fetch_add(1, Ordering::Relaxed);
        }
    }
    /// Never hold the queue lock during USB/file I/O or allocation. Limit each
    /// drain so the control loop can still handle commands under sustained RF.
    pub fn drain(&self, out: &mut impl Write) -> io::Result<()> {
        for _ in 0..256 {
            let r = {
                let mut guard = self
                    .queue
                    .lock()
                    .map_err(|_| io::Error::other("recorder poisoned"))?;
                let Some(q) = guard.as_mut() else {
                    return Ok(());
                };
                if q.len == 0 {
                    return Ok(());
                }
                let r = q.slots[q.head];
                q.head = (q.head + 1) % q.slots.len();
                q.len -= 1;
                r
            };
            r.write(out)?;
        }
        Ok(())
    }
    pub fn finish(&self, now: u64, out: &mut impl Write) -> io::Result<()> {
        self.accepting.store(false, Ordering::SeqCst);
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
        while self.active.load(Ordering::SeqCst) != 0 {
            if std::time::Instant::now() > deadline {
                return Err(io::Error::other("recorder callback not quiescent"));
            }
            // Allow a lower-priority native worker to finish on fixed-priority targets.
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        self.enabled.store(false, Ordering::Release);
        self.drain(out)?;
        let mut guard = self
            .queue
            .lock()
            .map_err(|_| io::Error::other("recorder poisoned"))?;
        let q = guard
            .take()
            .ok_or_else(|| io::Error::other("recorder not prepared"))?;
        let mut end = Record::new(END, now, &[q.next, q.chain, self.loss()]).unwrap();
        end.sequence = q.next;
        end.write(out)?;
        out.flush()
    }
}

pub struct Lease<'a>(&'a Recorder);
impl Lease<'_> {
    pub fn push(&self, r: Record) {
        self.0.push_admitted(r);
    }
}
impl Drop for Lease<'_> {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Ordering::SeqCst);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::calibration::Config;
    #[test]
    fn overflow_and_preempted_consumer_never_wait_or_silently_pass() {
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 1));
        r.push(Record::new(journal::LABEL, 1, &[1]).unwrap());
        assert_eq!(r.loss(), 1);
        let lock = r.queue.lock().unwrap();
        r.push(Record::new(journal::LABEL, 1, &[1]).unwrap());
        assert_eq!(r.loss(), 2);
        drop(lock);
        let mut out = Vec::new();
        r.finish(2, &mut out).unwrap();
        let mut integrity = journal::Integrity::default();
        let mut failure = false;
        for s in std::str::from_utf8(&out).unwrap().lines() {
            if integrity.accept(&Record::parse(s).unwrap()).is_err() {
                failure = true;
            }
        }
        assert!(failure);
    }
    #[test]
    fn finish_waits_for_native_call_lease_and_rejects_late_admission() {
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 8));
        let lease = r.lease().unwrap();
        std::thread::scope(|scope| {
            let owner = scope.spawn(|| {
                let mut out = Vec::new();
                r.finish(10, &mut out).unwrap();
                out
            });
            while r.accepting.load(Ordering::Acquire) {
                std::thread::yield_now();
            }
            assert!(r.lease().is_none());
            lease.push(Record::new(journal::LABEL, 2, &[1]).unwrap());
            drop(lease);
            let out = owner.join().unwrap();
            let mut integrity = journal::Integrity::default();
            for s in std::str::from_utf8(&out).unwrap().lines() {
                integrity.accept(&Record::parse(s).unwrap()).unwrap();
            }
            integrity.finish().unwrap();
        });
    }
    #[test]
    fn busy_recorded_poll_does_not_poison_source_or_sample_a_clock() {
        use crate::calibration::{Runtime, State};
        let r = Runtime::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT));
        r.update(|_| {
            assert!(r
                .execute(
                    journal::Operation::Poll,
                    || panic!("busy poll clock"),
                    |_| panic!("busy poll record")
                )
                .is_none());
        })
        .unwrap();
        assert_eq!(r.snapshot(|| 0).unwrap().state, State::Unknown);
        assert_eq!(
            r.snapshot(|| 0).unwrap().rejection,
            crate::calibration::Rejection::None
        );
    }
}
