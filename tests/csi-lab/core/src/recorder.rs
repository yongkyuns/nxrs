//! Callback-safe bounded transport: allocate on prepare, try-lock on producer,
//! format/write only on the owner task. Any overflow/contention is permanent loss.
use crate::journal::{self, Record, END};
use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Mutex;
// Each generation has one reserving producer and then one consumer. Per-slot
// mutexes keep the storage entirely safe Rust; their uncontended admission is
// guaranteed by generation ownership, rather than a global callback lock.
struct Slot {
    generation: AtomicU32,
    value: Mutex<Record>,
}
#[derive(Default)]
struct Consumer {
    next: u32,
    chain: u32,
}
pub struct Recorder {
    enabled: AtomicBool,
    accepting: AtomicBool,
    lost: AtomicU32,
    active: AtomicU32,
    serial: AtomicU32,
    storage: std::sync::OnceLock<Box<[Slot]>>,
    consumer: Mutex<Consumer>,
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
            serial: AtomicU32::new(0),
            storage: std::sync::OnceLock::new(),
            consumer: Mutex::new(Consumer { next: 0, chain: 0 }),
        }
    }
    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Acquire)
    }
    pub const fn storage_bytes(capacity: usize) -> usize {
        capacity * std::mem::size_of::<Slot>()
    }
    pub fn prepare(&self, config: crate::calibration::Config, capacity: usize) -> bool {
        if capacity == 0 || capacity > 256 || self.storage.get().is_some() {
            return false;
        }
        let slots = (0..capacity)
            .map(|i| Slot {
                generation: AtomicU32::new(2 * i as u32),
                value: Mutex::new(Record::EMPTY),
            })
            .collect::<Vec<_>>()
            .into_boxed_slice();
        // Materialize every native mutex on the owner task, before callbacks.
        for slot in &slots {
            if slot.value.lock().is_err() {
                return false;
            }
        }
        if self.consumer.lock().is_err() || self.storage.set(slots).is_err() {
            return false;
        }
        self.enabled.store(true, Ordering::Release);
        self.accepting.store(true, Ordering::SeqCst);
        self.push(journal::session(config));
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
    pub fn push_model(&self, event: journal::ModelEvent) {
        self.push_words(journal::MODEL, event.time_us, event.words.len(), |words| {
            words.copy_from_slice(&event.words)
        });
    }
    /// Fill a bounded owned slot in place. The fill closure runs synchronously
    /// under try-lock admission; SDK pointers are never retained past the call.
    pub fn push_words(&self, kind: u32, time_us: u64, len: usize, fill: impl FnOnce(&mut [u32])) {
        if let Some(lease) = self.lease() {
            lease.push_words(kind, time_us, len, fill);
        }
    }
    #[inline(never)]
    fn append_admitted(&self, kind: u32, time_us: u64, len: usize, fill: impl FnOnce(&mut [u32])) {
        if len > journal::WORDS || !(journal::SESSION..=journal::CONTROL).contains(&kind) {
            self.invalidate();
            return;
        }
        let Some(slots) = self.storage.get() else {
            self.invalidate();
            return;
        };
        let serial = self.serial.fetch_add(1, Ordering::Relaxed);
        if serial > u32::MAX / 2 - 256 {
            self.invalidate();
            return;
        }
        let slot = &slots[serial as usize % slots.len()];
        // Even tags are free; odd tags are published. Two phases also make
        // capacity one unambiguous. A failed reservation invalidates the epoch.
        if slot.generation.load(Ordering::Acquire) != 2 * serial {
            self.invalidate();
            return;
        }
        let Ok(mut r) = slot.value.try_lock() else {
            self.invalidate();
            return;
        };
        r.sequence = serial;
        r.time_us = time_us;
        r.kind = kind;
        r.len = len;
        fill(&mut r.words[..len]);
        drop(r);
        slot.generation.store(2 * serial + 1, Ordering::Release);
    }
    pub fn loss(&self) -> u32 {
        self.lost.load(Ordering::Acquire)
    }
    pub fn invalidate(&self) {
        if self.enabled() {
            self.lost.fetch_add(1, Ordering::Relaxed);
        }
    }
    /// One consumer owns output order and checksums. Producers never acquire
    /// this ownership mutex, and slot ownership is released before any I/O.
    pub fn drain(&self, out: &mut impl Write) -> io::Result<()> {
        let Some(slots) = self.storage.get() else {
            return Ok(());
        };
        let mut consumer = self
            .consumer
            .lock()
            .map_err(|_| io::Error::other("recorder consumer poisoned"))?;
        for _ in 0..256 {
            let serial = consumer.next;
            let slot = &slots[serial as usize % slots.len()];
            if slot.generation.load(Ordering::Acquire) != 2 * serial + 1 {
                return Ok(());
            }
            let r = {
                let value = slot
                    .value
                    .try_lock()
                    .map_err(|_| io::Error::other("published recorder slot busy"))?;
                *value
            };
            // The value guard has retired before a producer may reuse storage.
            slot.generation
                .store(2 * (serial + slots.len() as u32), Ordering::Release);
            consumer.next += 1;
            let crc = r.checksum();
            r.write_crc(out, crc)?;
            consumer.chain = journal::chain(consumer.chain, crc);
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
        let consumer = self
            .consumer
            .lock()
            .map_err(|_| io::Error::other("recorder consumer poisoned"))?;
        let mut end = Record::new(END, now, &[consumer.next, consumer.chain, self.loss()]).unwrap();
        end.sequence = consumer.next;
        end.write(out)?;
        out.flush()
    }
}

pub struct Lease<'a>(&'a Recorder);
impl Lease<'_> {
    /// One bounded record for a short native vector comparison. Capture inputs
    /// before execution and publish only after its result is stored. A rejected
    /// reservation still invokes the unchanged native operation exactly once.
    #[inline(never)]
    pub fn wander(
        &self,
        id: u32,
        now: u64,
        a: &[f32],
        b: &[f32],
        compute: impl FnOnce() -> f32,
    ) -> f32 {
        let n = a.len();
        if n == 0 || n != b.len() || n > journal::PACKED_WANDER_COLS {
            self.0.invalidate();
            return compute();
        }
        let mut compute = Some(compute);
        let mut result = None;
        self.push_words(journal::WANDER, now, 9 + 2 * n, |words| {
            words[..8].copy_from_slice(&[id, 3, n as u32, 0, 0, (2 * n + 1) as u32, 0, 0]);
            for (to, from) in words[8..8 + n].iter_mut().zip(a) {
                *to = from.to_bits();
            }
            for (to, from) in words[8 + n..8 + 2 * n].iter_mut().zip(b) {
                *to = from.to_bits();
            }
            let value = compute.take().unwrap()();
            words[8 + 2 * n] = value.to_bits();
            result = Some(value);
        });
        result.unwrap_or_else(|| compute.take().unwrap()())
    }
    pub fn push_words(&self, kind: u32, time_us: u64, len: usize, fill: impl FnOnce(&mut [u32])) {
        self.0.append_admitted(kind, time_us, len, fill);
    }
    pub fn push(&self, r: Record) {
        self.push_words(r.kind, r.time_us, r.len, |words| {
            words.copy_from_slice(r.data())
        });
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
        let lock = r.consumer.lock().unwrap();
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

#[cfg(test)]
mod in_place_tests {
    use super::*;
    use crate::calibration::Config;
    #[test]
    fn in_place_capture_owns_reused_source_and_matches_the_wire_encoder() {
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 4));
        let mut source = [0xa5a5a5a5u32; journal::WORDS];
        source[0] = 7;
        r.push_words(journal::RAW, 123, source.len(), |out| {
            out.copy_from_slice(&source)
        });
        source.fill(0);
        let mut out = Vec::new();
        r.finish(124, &mut out).unwrap();
        let text = std::str::from_utf8(&out).unwrap();
        let data = Record::parse(text.lines().nth(1).unwrap()).unwrap();
        assert_eq!(data.words[0], 7);
        assert_eq!(data.words[255], 0xa5a5a5a5);
        assert_eq!(data.time_us, 123);
        let mut check = journal::Integrity::default();
        for line in text.lines() {
            check.accept(&Record::parse(line).unwrap()).unwrap();
        }
        check.finish().unwrap();
    }
    #[test]
    fn callback_event_size_and_rejected_fill_do_not_require_a_maximum_record() {
        assert!(std::mem::size_of::<journal::ModelEvent>() <= 128);
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 1));
        r.push_words(journal::RAW, 0, 1, |_| {
            panic!("full queue must not invoke fill")
        });
        r.push_words(journal::RAW, 0, journal::WORDS + 1, |_| {
            panic!("invalid bounds must not invoke fill")
        });
        assert_eq!(r.loss(), 2);
    }
}

#[cfg(test)]
mod generation_tests {
    use super::*;
    use crate::calibration::Config;
    #[test]
    fn capacity_one_reuses_generations_without_confusing_published_and_free() {
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 1));
        let mut out = Vec::new();
        r.drain(&mut out).unwrap();
        for n in 1..=128 {
            r.push_words(journal::LABEL, n, 1, |w| w[0] = 1);
            r.drain(&mut out).unwrap();
        }
        r.finish(129, &mut out).unwrap();
        assert_eq!(r.loss(), 0);
        let mut integrity = journal::Integrity::default();
        for line in std::str::from_utf8(&out).unwrap().lines() {
            integrity.accept(&Record::parse(line).unwrap()).unwrap();
        }
        integrity.finish().unwrap();
        assert!(!r.prepare(Config::EMPTY_HOLDOUT, 1));
    }
    #[test]
    fn later_producer_publishes_without_waiting_for_preempted_reservation() {
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 4));
        let mut out = Vec::new();
        r.drain(&mut out).unwrap();
        let lease = r.lease().unwrap();
        let serial = r.serial.fetch_add(1, Ordering::Relaxed);
        assert_eq!(serial, 1);
        let slot = &r.storage.get().unwrap()[1];
        let mut value = slot.value.lock().unwrap();
        r.push_words(journal::LABEL, 2, 1, |words| words[0] = 2);
        assert_eq!(r.loss(), 0);
        r.drain(&mut out).unwrap();
        assert_eq!(r.consumer.lock().unwrap().next, 1);
        value.sequence = 1;
        value.time_us = 1;
        value.kind = journal::LABEL;
        value.len = 1;
        value.words[0] = 1;
        drop(value);
        slot.generation.store(3, Ordering::Release);
        drop(lease);
        r.finish(3, &mut out).unwrap();
        let records = std::str::from_utf8(&out)
            .unwrap()
            .lines()
            .map(|s| Record::parse(s).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(records[1].words[0], 1);
        assert_eq!(records[2].words[0], 2);
        let mut integrity = journal::Integrity::default();
        for record in records {
            integrity.accept(&record).unwrap();
        }
        integrity.finish().unwrap();
    }
    #[test]
    fn blocked_consumer_io_cannot_hold_a_producer_slot_or_global_queue_lock() {
        struct Blocked<'a> {
            start: &'a std::sync::Barrier,
            resume: &'a std::sync::Barrier,
            once: bool,
            bytes: Vec<u8>,
        }
        impl Write for Blocked<'_> {
            fn write(&mut self, b: &[u8]) -> io::Result<usize> {
                if !self.once {
                    self.once = true;
                    self.start.wait();
                    self.resume.wait();
                }
                self.bytes.extend_from_slice(b);
                Ok(b.len())
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 2));
        let start = std::sync::Barrier::new(2);
        let resume = std::sync::Barrier::new(2);
        std::thread::scope(|scope| {
            let consumer = scope.spawn(|| {
                let mut out = Blocked {
                    start: &start,
                    resume: &resume,
                    once: false,
                    bytes: Vec::new(),
                };
                r.drain(&mut out).unwrap();
                out.bytes
            });
            start.wait();
            r.push_words(journal::LABEL, 1, 1, |w| w[0] = 1);
            r.push_words(journal::LABEL, 2, 1, |w| w[0] = 2);
            assert_eq!(r.loss(), 0);
            resume.wait();
            let mut out = consumer.join().unwrap();
            r.finish(3, &mut out).unwrap();
            let mut integrity = journal::Integrity::default();
            for line in std::str::from_utf8(&out).unwrap().lines() {
                integrity.accept(&Record::parse(line).unwrap()).unwrap();
            }
            integrity.finish().unwrap();
        });
    }
}

#[cfg(test)]
mod packed_wander_tests {
    use super::*;
    use crate::calibration::Config;
    fn records(r: &Recorder) -> Vec<Record> {
        let mut out = Vec::new();
        r.finish(1000, &mut out).unwrap();
        std::str::from_utf8(&out)
            .unwrap()
            .lines()
            .map(|v| Record::parse(v).unwrap())
            .collect()
    }
    #[test]
    fn training_comparison_burst_fits_unchanged_slots_with_exact_owned_inputs() {
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 128));
        let mut a = [1.0; 13];
        let b = [2.0; 13];
        let calls = std::cell::Cell::new(0);
        for id in 1..=100 {
            let lease = r.lease().unwrap();
            let value = lease.wander(id, id as u64, &a, &b, || {
                calls.set(calls.get() + 1);
                -0.0
            });
            assert_eq!(value.to_bits(), (-0.0f32).to_bits());
        }
        a.fill(0.0);
        assert_eq!(calls.get(), 100);
        assert_eq!(r.loss(), 0);
        let records = records(&r);
        assert_eq!(records.len(), 102);
        for record in &records[1..101] {
            assert_eq!(record.len, 35);
            assert_eq!(&record.words[8..21], &[1.0f32.to_bits(); 13]);
            assert_eq!(&record.words[21..34], &[2.0f32.to_bits(); 13]);
            assert_eq!(record.words[34], (-0.0f32).to_bits());
        }
        let mut integrity = journal::Integrity::default();
        for record in records {
            integrity.accept(&record).unwrap();
        }
        integrity.finish().unwrap();
    }
    #[test]
    fn full_or_invalid_reservation_preserves_one_native_call_and_sticky_loss() {
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 1));
        let lease = r.lease().unwrap();
        let calls = std::cell::Cell::new(0);
        for (a, b) in [
            (&[1.0][..], &[2.0][..]),
            (&[][..], &[][..]),
            (&[1.0][..], &[][..]),
        ] {
            let result = lease.wander(1, 1, a, b, || {
                calls.set(calls.get() + 1);
                0.5
            });
            assert_eq!(result, 0.5);
        }
        assert_eq!(calls.get(), 3);
        assert_eq!(r.loss(), 3);
    }
    #[test]
    fn maximum_packed_input_and_nonfinite_native_result_keep_exact_bits() {
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 4));
        let lease = r.lease().unwrap();
        let bits = 0x7fc01234;
        let a = [1.0; journal::PACKED_WANDER_COLS];
        assert_eq!(
            lease
                .wander(1, 1, &a, &a, || f32::from_bits(bits))
                .to_bits(),
            bits
        );
        drop(lease);
        let records = records(&r);
        assert_eq!(records[1].len, 255);
        assert_eq!(records[1].words[254], bits);
    }
    #[test]
    fn end_waits_for_packed_native_execution_before_publishing_complete_call() {
        let r = Recorder::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT, 4));
        let admitted = std::sync::Barrier::new(2);
        let resume = std::sync::Barrier::new(2);
        std::thread::scope(|scope| {
            let native = scope.spawn(|| {
                let lease = r.lease().unwrap();
                lease.wander(1, 1, &[1.0], &[2.0], || {
                    admitted.wait();
                    resume.wait();
                    0.25
                })
            });
            admitted.wait();
            let owner = scope.spawn(|| records(&r));
            while r.accepting.load(Ordering::SeqCst) {
                std::thread::yield_now();
            }
            assert!(r.lease().is_none());
            resume.wait();
            assert_eq!(native.join().unwrap(), 0.25);
            let rows = owner.join().unwrap();
            assert_eq!(rows.len(), 3);
            assert_eq!(rows[1].words[10], 0.25f32.to_bits());
        });
    }
}
