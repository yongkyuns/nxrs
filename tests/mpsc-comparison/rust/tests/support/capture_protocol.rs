//! Host-only capture admission protocol; std MPSC is a payload test adapter.
//! No native queue-progress, timing, wire, or production integration claim.
#![forbid(unsafe_code)]

use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};
use std::marker::PhantomData;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::*};
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError};
use std::thread::{self, Thread};
use std::time::{Duration, Instant};

use super::capture_gate::{
    CAPACITY, Claim as GateClaim, Closing as GateClosing, Credit, Gate as Shared, Key, MAX_EPOCH,
    Phase, Rejection, StartError, Status,
};

// Deliberately neither Copy nor Clone. The external oracle retains its drop
// witness; queue ownership and credit release are not inferred from Drop.
struct Payload {
    key: Key,
    bits: u64,
    drops: Arc<AtomicUsize>,
}
impl Drop for Payload {
    fn drop(&mut self) {
        self.drops.fetch_add(1, SeqCst);
    }
}
fn bits(key: Key) -> u64 {
    (u64::from(key.sequence) << 17) ^ u64::from(key.source) ^ 0xa15e_4460
}

struct Envelope {
    epoch: u32,
    payload: Payload,
}

struct Publisher {
    source: u8,
    shared: Arc<Shared>,
    sender: SyncSender<Envelope>,
    _exclusive: PhantomData<Cell<()>>,
}

struct Owner {
    shared: Arc<Shared>,
    receiver: Receiver<Envelope>,
}

struct Rejected {
    reason: Rejection,
    payload: Payload,
}
enum Claim<'a> {
    Accepted(Permit<'a>),
    Full(Closing<'a>),
    Rejected(Rejected),
}

struct Permit<'a> {
    credit: Credit<'a>,
    sender: &'a SyncSender<Envelope>,
    epoch: u32,
    payload: Option<Payload>,
}

struct Closing<'a> {
    closing: GateClosing<'a>,
    payload: Option<Payload>,
}

fn channel(initial_epoch: u32, owner: Thread) -> (Owner, [Publisher; 3]) {
    let shared = Arc::new(Shared::new(initial_epoch, owner));
    let (sender, receiver) = mpsc::sync_channel(CAPACITY as usize);
    let publishers = std::array::from_fn(|source| Publisher {
        source: source as u8,
        shared: shared.clone(),
        sender: sender.clone(),
        _exclusive: PhantomData,
    });
    (Owner { shared, receiver }, publishers)
}

impl Publisher {
    fn captured_epoch(&self) -> u32 {
        self.shared.captured_epoch()
    }

    fn reserve(&mut self, captured_epoch: u32, payload: Payload) -> Claim<'_> {
        self.reserve_with(captured_epoch, payload, || {})
    }

    fn reserve_with(
        &mut self,
        captured_epoch: u32,
        payload: Payload,
        before_compare: impl FnOnce(),
    ) -> Claim<'_> {
        assert_eq!(payload.key.source, self.source);
        match self.shared.reserve_with(
            self.source,
            captured_epoch,
            payload.key.sequence,
            before_compare,
        ) {
            GateClaim::Accepted(credit) => Claim::Accepted(Permit {
                credit,
                sender: &self.sender,
                epoch: captured_epoch,
                payload: Some(payload),
            }),
            GateClaim::Full(closing) => Claim::Full(Closing {
                closing,
                payload: Some(payload),
            }),
            GateClaim::Rejected(reason) => Claim::Rejected(Rejected { reason, payload }),
        }
    }
}

impl Permit<'_> {
    fn commit(self) {
        self.commit_with(|| {}, || {});
    }

    // Hooks are fixture-only schedule boundaries, not a proposed product API.
    fn commit_with(mut self, after_send: impl FnOnce(), after_ready: impl FnOnce()) {
        let envelope = Envelope {
            epoch: self.epoch,
            payload: self.payload.take().unwrap(),
        };
        if let Err(error) = self.sender.try_send(envelope) {
            self.payload = Some(match error {
                mpsc::TrySendError::Full(value) | mpsc::TrySendError::Disconnected(value) => {
                    value.payload
                }
            });
            panic!("reserved pooled credit could not commit to its live payload adapter");
        }
        // Storage owns the payload before any hook can unwind or the source
        // obligation can retire. Only the consumer returns a published credit.
        self.credit.mark_published();
        after_send();
        self.credit.finish_with(after_ready);
    }
}

impl Closing<'_> {
    fn publish(mut self) -> Rejected {
        let payload = self.payload.take().unwrap();
        self.closing.publish();
        Rejected {
            reason: Rejection::Full,
            payload,
        }
    }
}

impl Owner {
    fn begin(&mut self) -> Result<u32, StartError> {
        if self.shared.phase() != Phase::Inactive || self.shared.obligations() != (0, 0) {
            return Err(StartError::Pending);
        }
        if self.shared.captured_epoch() == MAX_EPOCH {
            return Err(StartError::Exhausted);
        }
        match self.receiver.try_recv() {
            Err(TryRecvError::Empty) => {}
            Err(TryRecvError::Disconnected) => return Err(StartError::Disconnected),
            Ok(_) => panic!("new epoch would overwrite retained payload"),
        }
        self.shared.begin()
    }

    fn cancel(&mut self) {
        self.shared.cancel();
    }

    fn drain(&mut self, limit: usize) -> Vec<Payload> {
        let mut values = Vec::new();
        for _ in 0..limit {
            let envelope = match self.receiver.try_recv() {
                Ok(value) => value,
                Err(TryRecvError::Empty | TryRecvError::Disconnected) => break,
            };
            assert_eq!(
                self.shared.captured_epoch(),
                envelope.epoch,
                "old payload crossed an epoch boundary"
            );
            self.shared.release_credit();
            values.push(envelope.payload);
        }
        values
    }

    fn status(&mut self) -> Status {
        let status = self.shared.status();
        if matches!(status, Status::Overflow(_) | Status::Cancelled) {
            assert!(matches!(
                self.receiver.try_recv(),
                Err(TryRecvError::Empty | TryRecvError::Disconnected)
            ));
        }
        status
    }

    fn finish(&mut self) -> Result<Status, StartError> {
        self.status();
        self.shared.finish()
    }

    fn take_ready(&mut self) -> bool {
        self.shared.take_ready()
    }
    fn obligations(&self) -> (u32, u32) {
        self.shared.obligations()
    }
}

#[derive(Clone, Copy, Debug)]
struct Attempt {
    key: Key,
    bits: u64,
    result: Result<(), Rejection>,
}

fn attempt(publisher: &mut Publisher, captured_epoch: u32, payload: Payload) -> Attempt {
    let key = payload.key;
    let original_bits = payload.bits;
    let original_owned = payload.drops.clone();
    let rejected = match publisher.reserve(captured_epoch, payload) {
        Claim::Accepted(permit) => {
            permit.commit();
            None
        }
        Claim::Full(closing) => Some(closing.publish()),
        Claim::Rejected(rejected) => Some(rejected),
    };
    let result = rejected.map_or(Ok(()), |rejected| {
        assert_eq!(rejected.payload.key, key);
        assert_eq!(
            rejected.payload.bits, original_bits,
            "rejection replaced owned payload"
        );
        assert!(
            Arc::ptr_eq(&original_owned, &rejected.payload.drops),
            "rejection replaced ownership with equal scalars"
        );
        Err(rejected.reason)
    });
    Attempt {
        key,
        bits: original_bits,
        result,
    }
}

#[derive(Default)]
struct Audit {
    expected: BTreeMap<Key, Arc<AtomicUsize>>,
    attempts: BTreeMap<Key, Result<(), Rejection>>,
    delivered: Vec<Key>,
    discarded: BTreeSet<Key>,
}

impl Audit {
    fn payload(&mut self, source: u8, sequence: u32) -> Payload {
        let key = Key { source, sequence };
        let drops = Arc::new(AtomicUsize::new(0));
        assert!(self.expected.insert(key, drops.clone()).is_none());
        Payload {
            key,
            bits: bits(key),
            drops,
        }
    }
    fn attempt(&mut self, observed: Attempt) {
        assert_eq!(observed.bits, bits(observed.key));
        assert!(self.expected.contains_key(&observed.key));
        assert!(
            self.attempts
                .insert(observed.key, observed.result)
                .is_none()
        );
    }
    fn accepted(&mut self, key: Key) {
        self.attempt(Attempt {
            key,
            bits: bits(key),
            result: Ok(()),
        });
    }
    fn received(&mut self, payloads: Vec<Payload>, discard: bool) {
        for payload in payloads {
            assert_eq!(payload.bits, bits(payload.key));
            assert!(self.expected.contains_key(&payload.key));
            assert!(
                Arc::ptr_eq(&self.expected[&payload.key], &payload.drops),
                "a different epoch's owned value reused this source/sequence"
            );
            if discard {
                assert!(self.discarded.insert(payload.key));
            } else {
                self.delivered.push(payload.key);
            }
        }
    }
    fn check(&self, retained: &[Key]) -> (usize, usize, usize, usize) {
        assert_eq!(self.expected.len(), self.attempts.len());
        let admitted: BTreeSet<_> = self
            .attempts
            .iter()
            .filter_map(|(key, result)| result.is_ok().then_some(*key))
            .collect();
        let delivered: BTreeSet<_> = self.delivered.iter().copied().collect();
        assert_eq!(delivered.len(), self.delivered.len(), "duplicate delivery");
        assert!(delivered.is_disjoint(&self.discarded));
        let retained: BTreeSet<_> = retained.iter().copied().collect();
        assert!(retained.is_disjoint(&delivered) && retained.is_disjoint(&self.discarded));
        let accounted: BTreeSet<_> = delivered
            .union(&self.discarded)
            .copied()
            .chain(retained.iter().copied())
            .collect();
        assert_eq!(
            admitted, accounted,
            "admitted records were lost or fabricated"
        );
        for source in 0..3 {
            let sequences: Vec<_> = self
                .delivered
                .iter()
                .filter(|key| key.source == source)
                .map(|key| key.sequence)
                .collect();
            assert!(
                sequences.windows(2).all(|pair| pair[0] < pair[1]),
                "per-source FIFO changed"
            );
        }
        for (key, drops) in &self.expected {
            assert_eq!(
                drops.load(SeqCst),
                usize::from(!retained.contains(key)),
                "owned value not dropped exactly once: {key:?}"
            );
        }
        (
            admitted.len(),
            delivered.len(),
            self.discarded.len(),
            retained.len(),
        )
    }
}

fn report(case: &str, audit: &Audit, retained: &[Key]) {
    let (admitted, delivered, discarded, retained) = audit.check(retained);
    let count = |reason| {
        audit
            .attempts
            .values()
            .filter(|result| **result == Err(reason))
            .count()
    };
    let (full, closed, busy) = (
        count(Rejection::Full),
        count(Rejection::Closed),
        count(Rejection::Busy),
    );
    println!(
        "CAPTURE_PROTOCOL {{\"case\":\"{case}\",\"runtime\":\"host\",\"payload_adapter\":\"desktop-std-bounded-mpsc\",\"shared_capacity\":512,\"admitted\":{admitted},\"delivered\":{delivered},\"discarded\":{discarded},\"retained\":{retained},\"rejected_full\":{full},\"rejected_closed\":{closed},\"rejected_busy\":{busy},\"production_migrated\":false,\"native_progress_qualified\":false,\"success\":true}}"
    );
}

fn completed(owner: &mut Owner, terminal: Status) {
    assert_eq!(owner.status(), terminal);
    assert_eq!(
        owner.status(),
        terminal,
        "terminal observation must be sticky"
    );
    assert_eq!(owner.begin(), Err(StartError::Pending));
    assert_eq!(owner.finish(), Ok(terminal));
    assert_eq!(owner.status(), Status::Inactive);
}

fn wait<T>(receiver: &Receiver<T>) -> T {
    receiver
        .recv_timeout(Duration::from_secs(2))
        .expect("scripted protocol boundary timed out")
}

fn serial(case: &str, offers: Vec<Key>) {
    let (mut owner, mut publishers) = channel(0, thread::current());
    let epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    for (index, key) in offers.iter().enumerate() {
        let payload = audit.payload(key.source, key.sequence);
        let result = attempt(&mut publishers[usize::from(key.source)], epoch, payload);
        assert_eq!(
            result.result,
            if index < 512 {
                Ok(())
            } else if index == 512 {
                Err(Rejection::Full)
            } else {
                Err(Rejection::Closed)
            }
        );
        audit.attempt(result);
        assert_eq!(owner.obligations(), ((index + 1).min(512) as u32, 0));
    }
    while owner.obligations().0 != 0 {
        audit.received(owner.drain(16), false);
    }
    assert_eq!(audit.delivered, offers[..offers.len().min(512)]);
    let terminal = if offers.len() > 512 {
        Status::Overflow(offers[512])
    } else {
        owner.cancel();
        Status::Cancelled
    };
    completed(&mut owner, terminal);
    report(case, &audit, &[]);
}

fn nominal(ticks: u32) -> Vec<Key> {
    let mut values = Vec::new();
    for tick in 0..ticks {
        values.push(Key {
            source: 0,
            sequence: tick,
        });
        values.push(Key {
            source: 1,
            sequence: tick,
        });
        if tick % 50 == 0 {
            values.push(Key {
                source: 2,
                sequence: tick / 50,
            });
        }
    }
    values
}

fn paused_admission(after_commit: bool) {
    let (mut owner, [mut low, mut filler, mut closer]) = channel(0, thread::current());
    let epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    for sequence in 0..511 {
        let payload = audit.payload(1, sequence);
        audit.attempt(attempt(&mut filler, epoch, payload));
    }
    let payload = audit.payload(0, 0);
    let key = payload.key;
    let (entered, observed) = mpsc::sync_channel(0);
    let (resume, resumed) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let worker = scope.spawn(move || {
            let Claim::Accepted(permit) = low.reserve(epoch, payload) else {
                panic!("missing reserved credit")
            };
            let pause = || {
                entered.send(()).unwrap();
                wait(&resumed);
            };
            if after_commit {
                permit.commit_with(pause, || {});
            } else {
                pause();
                permit.commit();
            }
        });
        wait(&observed);
        audit.accepted(key);
        let rejected = audit.payload(2, 0);
        audit.attempt(attempt(&mut closer, epoch, rejected));
        audit.received(owner.drain(512), false);
        assert_eq!(owner.obligations(), (u32::from(!after_commit), 1));
        assert_eq!(owner.status(), Status::Pending);
        assert_eq!(owner.finish(), Err(StartError::Pending));
        assert_eq!(owner.begin(), Err(StartError::Pending));
        // Pending observation itself neither spins nor schedules another turn.
        owner.take_ready();
        assert_eq!(owner.status(), Status::Pending);
        assert!(!owner.take_ready());
        resume.send(()).unwrap();
        worker.join().unwrap();
    });
    audit.received(owner.drain(512), false);
    completed(
        &mut owner,
        Status::Overflow(Key {
            source: 2,
            sequence: 0,
        }),
    );
    report(
        if after_commit {
            "dequeued-before-publisher-retirement"
        } else {
            "paused-reservation-before-commit"
        },
        &audit,
        &[],
    );
}

fn simultaneous_overflow(initial: u32) {
    let (mut owner, [mut first, mut second, mut filler]) = channel(0, thread::current());
    let epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    for sequence in 0..initial {
        let payload = audit.payload(2, sequence);
        audit.attempt(attempt(&mut filler, epoch, payload));
    }
    let a = audit.payload(0, 0);
    let b = audit.payload(1, 0);
    let start = std::sync::Barrier::new(3);
    let results = thread::scope(|scope| {
        let start = &start;
        let a = scope.spawn(move || {
            start.wait();
            attempt(&mut first, epoch, a)
        });
        let b = scope.spawn(move || {
            start.wait();
            attempt(&mut second, epoch, b)
        });
        start.wait();
        [a.join().unwrap(), b.join().unwrap()]
    });
    assert_eq!(
        results
            .iter()
            .filter(|result| result.result == Err(Rejection::Full))
            .count(),
        1
    );
    assert_eq!(
        results
            .iter()
            .filter(|result| result.result == Err(Rejection::Closed))
            .count(),
        usize::from(initial == 512)
    );
    assert_eq!(
        results
            .iter()
            .filter(|result| result.result.is_ok())
            .count(),
        usize::from(initial == 511)
    );
    let winner = results
        .iter()
        .find(|result| result.result == Err(Rejection::Full))
        .unwrap()
        .key;
    for result in results {
        audit.attempt(result);
    }
    audit.received(owner.drain(512), false);
    completed(&mut owner, Status::Overflow(winner));
    report(
        if initial == 512 {
            "simultaneous-overflow-winner"
        } else {
            "race-for-last-shared-credit"
        },
        &audit,
        &[],
    );
}

fn paused_marker() {
    let (mut owner, [mut closer, mut filler, mut late]) = channel(0, thread::current());
    let epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    for sequence in 0..512 {
        let payload = audit.payload(1, sequence);
        audit.attempt(attempt(&mut filler, epoch, payload));
    }
    let payload = audit.payload(0, 0);
    let (entered, observed) = mpsc::sync_channel(0);
    let (resume, resumed) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let worker = scope.spawn(move || {
            let Claim::Full(closing) = closer.reserve(epoch, payload) else {
                panic!("full did not close")
            };
            entered.send(()).unwrap();
            wait(&resumed);
            let rejected = closing.publish();
            Attempt {
                key: rejected.payload.key,
                bits: rejected.payload.bits,
                result: Err(rejected.reason),
            }
        });
        wait(&observed);
        audit.received(owner.drain(512), false);
        assert_eq!(owner.obligations(), (0, 1));
        assert_eq!(owner.shared.marker(), None);
        assert_eq!(owner.status(), Status::Pending);
        assert_eq!(owner.begin(), Err(StartError::Pending));
        let payload = audit.payload(2, 0);
        audit.attempt(attempt(&mut late, epoch, payload));
        resume.send(()).unwrap();
        audit.attempt(worker.join().unwrap());
    });
    completed(
        &mut owner,
        Status::Overflow(Key {
            source: 0,
            sequence: 0,
        }),
    );
    report("paused-overflow-marker", &audit, &[]);
}

fn returned_credit_before_close() {
    let (mut owner, [mut contender, mut filler, _]) = channel(0, thread::current());
    let epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    for sequence in 0..512 {
        let payload = audit.payload(1, sequence);
        audit.attempt(attempt(&mut filler, epoch, payload));
    }
    let payload = audit.payload(0, 0);
    let key = payload.key;
    let (entered, observed) = mpsc::sync_channel(0);
    let (resume, resumed) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let worker = scope.spawn(move || {
            let claim = contender.reserve_with(epoch, payload, || {
                entered.send(()).unwrap();
                wait(&resumed);
            });
            let Claim::Accepted(permit) = claim else {
                panic!("stale full snapshot closed despite returned credit")
            };
            permit.commit();
        });
        wait(&observed);
        audit.received(owner.drain(1), false);
        assert_eq!(owner.obligations(), (511, 0));
        resume.send(()).unwrap();
        worker.join().unwrap();
    });
    audit.accepted(key);
    assert_eq!(owner.status(), Status::Open);
    assert_eq!(owner.obligations(), (512, 0));
    audit.received(owner.drain(512), false);
    owner.cancel();
    completed(&mut owner, Status::Cancelled);
    report("returned-credit-invalidates-full-snapshot", &audit, &[]);
}

fn cancelled_restart() {
    let (mut owner, [mut publisher, mut stale, _]) = channel(0, thread::current());
    let first_epoch = owner.begin().unwrap();
    assert_eq!(publisher.captured_epoch(), first_epoch);
    let mut old = Audit::default();
    let payload = old.payload(0, 7);
    let key = payload.key;
    let (entered, observed) = mpsc::sync_channel(0);
    let (resume, resumed) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let publisher = &mut publisher;
        let worker = scope.spawn(move || {
            let Claim::Accepted(permit) = publisher.reserve(first_epoch, payload) else {
                panic!("missing admission")
            };
            entered.send(()).unwrap();
            wait(&resumed);
            permit.commit();
        });
        wait(&observed);
        old.accepted(key);
        owner.cancel();
        assert!(owner.drain(16).is_empty());
        assert_eq!(owner.status(), Status::Pending);
        assert_eq!(owner.begin(), Err(StartError::Pending));
        resume.send(()).unwrap();
        worker.join().unwrap();
    });
    old.received(owner.drain(512), true);
    completed(&mut owner, Status::Cancelled);
    let next_epoch = owner.begin().unwrap();
    assert_ne!(first_epoch, next_epoch);
    let payload = old.payload(1, 7);
    let result = attempt(&mut stale, first_epoch, payload);
    assert_eq!(result.result, Err(Rejection::Closed));
    old.attempt(result);
    report("cancel-retire-old-epoch", &old, &[]);

    // Both identities deliberately repeat old source/sequence values. The
    // oracle checks their distinct owned witnesses, not just equal scalars.
    let mut new = Audit::default();
    let payload = new.payload(0, 7);
    new.attempt(attempt(&mut publisher, next_epoch, payload));
    let payload = new.payload(1, 7);
    new.attempt(attempt(&mut stale, next_epoch, payload));
    new.received(owner.drain(512), false);
    owner.cancel();
    completed(&mut owner, Status::Cancelled);
    report("restart-with-reused-source-sequences", &new, &[]);
}

fn close_between_snapshot_and_admission() {
    let (mut owner, [mut old_publisher, mut new_publisher, _]) = channel(0, thread::current());
    let old_epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    let payload = audit.payload(0, 0);
    let (entered, observed) = mpsc::sync_channel(0);
    let (resume, resumed) = mpsc::sync_channel(0);
    let new_epoch = thread::scope(|scope| {
        let worker = scope.spawn(move || {
            let Claim::Rejected(rejected) = old_publisher.reserve_with(old_epoch, payload, || {
                entered.send(()).unwrap();
                wait(&resumed);
            }) else {
                panic!("stale admission survived close and restart")
            };
            assert_eq!(rejected.reason, Rejection::Closed);
            Attempt {
                key: rejected.payload.key,
                bits: rejected.payload.bits,
                result: Err(rejected.reason),
            }
        });
        wait(&observed);
        owner.cancel();
        completed(&mut owner, Status::Cancelled);
        let next = owner.begin().unwrap();
        resume.send(()).unwrap();
        audit.attempt(worker.join().unwrap());
        next
    });
    let payload = audit.payload(1, 0);
    audit.attempt(attempt(&mut new_publisher, new_epoch, payload));
    audit.received(owner.drain(512), false);
    owner.cancel();
    completed(&mut owner, Status::Cancelled);
    report("close-restart-invalidates-admission-snapshot", &audit, &[]);
}

fn abandoned_permits(closing: bool) {
    let (mut owner, [mut abandoned, mut filler, mut closer]) = channel(0, thread::current());
    let epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    for sequence in 0..if closing { 512 } else { 511 } {
        let payload = audit.payload(1, sequence);
        audit.attempt(attempt(&mut filler, epoch, payload));
    }
    let payload = audit.payload(0, 0);
    let key = payload.key;
    if closing {
        let Claim::Full(permit) = abandoned.reserve(epoch, payload) else {
            panic!("expected closing obligation")
        };
        audit.attempt(Attempt {
            key,
            bits: bits(key),
            result: Err(Rejection::Full),
        });
        drop(permit);
    } else {
        let Claim::Accepted(permit) = abandoned.reserve(epoch, payload) else {
            panic!("expected admitted obligation")
        };
        audit.accepted(key);
        let full = audit.payload(2, 0);
        audit.attempt(attempt(&mut closer, epoch, full));
        assert_eq!(owner.shared.phase(), Phase::Overflow);
        drop(permit);
        assert!(audit.discarded.insert(key));
    }
    // A lost accepted value or missing terminal identity cancels the export;
    // neither path is allowed to report a successful overflow drain.
    assert_eq!(owner.shared.phase(), Phase::Cancelled);
    audit.received(owner.drain(512), true);
    completed(&mut owner, Status::Cancelled);
    report(
        if closing {
            "abandoned-closing-permit-cancels"
        } else {
            "abandoned-admission-cancels-overflow"
        },
        &audit,
        &[],
    );
}

fn forgotten_permit() {
    let (mut owner, [mut forgotten, mut other, mut late]) = channel(0, thread::current());
    let epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    let payload = audit.payload(0, 0);
    let key = payload.key;
    let Claim::Accepted(permit) = forgotten.reserve(epoch, payload) else {
        panic!("expected permit")
    };
    audit.accepted(key);
    std::mem::forget(permit);
    let payload = audit.payload(0, 1);
    let result = attempt(&mut forgotten, epoch, payload);
    assert_eq!(result.result, Err(Rejection::Busy));
    audit.attempt(result);
    let payload = audit.payload(1, 0);
    audit.attempt(attempt(&mut other, epoch, payload));
    audit.received(owner.drain(512), false);
    assert_eq!(owner.obligations(), (1, 1));
    owner.cancel();
    let payload = audit.payload(2, 0);
    let result = attempt(&mut late, epoch, payload);
    assert_eq!(result.result, Err(Rejection::Closed));
    audit.attempt(result);
    assert_eq!(owner.status(), Status::Pending);
    assert_eq!(owner.finish(), Err(StartError::Pending));
    assert_eq!(owner.begin(), Err(StartError::Pending));
    // This deliberate leak is reported as retained, never successful teardown.
    report("forgotten-permit-fails-closed", &audit, &[key]);
}

fn published_unwind_after_dequeue() {
    let (mut owner, [mut publisher, _second, _third]) = channel(0, thread::current());
    let epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    let payload = audit.payload(0, 0);
    let key = payload.key;
    let (sent, observed) = mpsc::sync_channel(0);
    let (resume, resumed) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let worker = scope.spawn(move || {
            let Claim::Accepted(permit) = publisher.reserve(epoch, payload) else {
                panic!("missing accepted value")
            };
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                permit.commit_with(
                    || {
                        sent.send(()).unwrap();
                        wait(&resumed);
                        std::panic::resume_unwind(Box::new("fixture after-send unwind"));
                    },
                    || {},
                );
            }));
            assert!(result.is_err());
        });
        wait(&observed);
        audit.accepted(key);
        audit.received(owner.drain(1), false);
        assert_eq!(owner.obligations(), (0, 1));
        assert_eq!(owner.status(), Status::Open);
        assert_eq!(owner.finish(), Err(StartError::Pending));
        resume.send(()).unwrap();
        worker.join().unwrap();
    });
    assert_eq!(owner.obligations(), (0, 0));
    assert_eq!(owner.shared.wake_count(), 1);
    completed(&mut owner, Status::Cancelled);
    report("published-unwind-after-dequeue", &audit, &[]);
}

fn epoch_exhaustion() {
    let (mut owner, [mut publisher, _, _]) = channel(MAX_EPOCH - 1, thread::current());
    let epoch = owner.begin().unwrap();
    assert_eq!(epoch, MAX_EPOCH);
    let mut audit = Audit::default();
    let payload = audit.payload(0, 0);
    audit.attempt(attempt(&mut publisher, epoch, payload));
    audit.received(owner.drain(512), false);
    owner.cancel();
    completed(&mut owner, Status::Cancelled);
    assert_eq!(owner.begin(), Err(StartError::Exhausted));
    assert_eq!(publisher.captured_epoch(), MAX_EPOCH);
    let payload = audit.payload(0, 1);
    let result = attempt(&mut publisher, 0, payload);
    assert_eq!(result.result, Err(Rejection::Closed));
    audit.attempt(result);
    report("epoch-exhaustion-fails-closed", &audit, &[]);
}

fn startup_rollback() {
    let (owner, [mut unattached, second, third]) = channel(0, thread::current());
    // Inject setup cancellation before opening; some endpoint handles have
    // already been handed out. No payload storage or admission is replaced.
    drop(second);
    drop(third);
    drop(owner);
    let mut audit = Audit::default();
    let payload = audit.payload(0, 0);
    let result = attempt(&mut unattached, 0, payload);
    assert_eq!(result.result, Err(Rejection::Closed));
    audit.attempt(result);
    report("startup-rollback-before-open", &audit, &[]);

    let (mut owner, publishers) = channel(0, thread::current());
    drop(publishers);
    assert_eq!(owner.begin(), Err(StartError::Disconnected));
    assert_eq!(owner.status(), Status::Inactive);
}

fn concurrent_source_fifo() {
    const ROUNDS: u32 = 4;
    const PER_SOURCE: u32 = 16;
    let (mut owner, publishers) = channel(0, thread::current());
    let epoch = owner.begin().unwrap();
    let mut audit = Audit::default();
    let payloads: [Vec<_>; 3] = std::array::from_fn(|source| {
        (0..ROUNDS * PER_SOURCE)
            .map(|sequence| audit.payload(source as u8, sequence))
            .collect()
    });
    let round = std::sync::Barrier::new(4);
    thread::scope(|scope| {
        let handles: Vec<_> = publishers
            .into_iter()
            .zip(payloads)
            .map(|(mut publisher, payloads)| {
                let round = &round;
                scope.spawn(move || {
                    let mut payloads = payloads.into_iter();
                    let mut results = Vec::new();
                    for _ in 0..ROUNDS {
                        round.wait();
                        for _ in 0..PER_SOURCE {
                            let result = attempt(&mut publisher, epoch, payloads.next().unwrap());
                            assert!(result.result.is_ok());
                            results.push(result);
                        }
                        round.wait();
                    }
                    results
                })
            })
            .collect();
        for _ in 0..ROUNDS {
            round.wait();
            let mut received = 0;
            let deadline = Instant::now() + Duration::from_secs(2);
            while received < 3 * PER_SOURCE {
                owner.take_ready();
                let batch = owner.drain(16);
                let count = batch.len() as u32;
                received += count;
                audit.received(batch, false);
                assert!(owner.obligations().0 <= CAPACITY);
                assert!(
                    Instant::now() < deadline,
                    "concurrent owner did not progress"
                );
                if count == 0 {
                    // A reserved but unpublished value is not a reason to
                    // self-notify. This owner uses a bounded wait/recheck.
                    thread::park_timeout(Duration::from_millis(1));
                }
            }
            round.wait();
            assert_eq!(owner.obligations(), (0, 0));
        }
        for handle in handles {
            for result in handle.join().unwrap() {
                audit.attempt(result);
            }
        }
    });
    owner.cancel();
    completed(&mut owner, Status::Cancelled);
    assert_eq!(owner.begin(), Err(StartError::Disconnected));
    report("concurrent-source-fifo-and-credit-return", &audit, &[]);
}

#[derive(Clone, Copy)]
enum WakeSchedule {
    Normal,
    Delayed,
    Unwind,
}

fn host_wake(schedule: WakeSchedule) {
    // These are bounded host owner-poll controls, not deterministic coverage
    // of the final recheck-to-park window or the product owner's wake loop.
    // Every witness has a fresh thread: earlier scenarios unpark their calling
    // thread, so reusing that thread would retain stale tokens.
    let (thread_tx, thread_rx) = mpsc::sync_channel(1);
    let (owner_tx, owner_rx) = mpsc::sync_channel::<Owner>(1);
    let (epoch_tx, epoch_rx) = mpsc::sync_channel(1);
    let (checked_tx, checked_rx) = mpsc::sync_channel(1);
    let (observed_tx, observed_rx) = mpsc::sync_channel(1);
    let worker = thread::spawn(move || {
        thread_tx.send(thread::current()).unwrap();
        let mut owner = wait(&owner_rx);
        let epoch = owner.begin().unwrap();
        epoch_tx.send(epoch).unwrap();
        assert!(!owner.take_ready());
        assert!(owner.drain(1).is_empty());
        checked_tx.try_send(()).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            owner.take_ready();
            let payloads = owner.drain(1);
            if !payloads.is_empty() {
                observed_tx.try_send(()).unwrap();
                return (owner, payloads);
            }
            assert!(Instant::now() < deadline, "host owner deadline expired");
            // Spurious returns are allowed. Only the retained queue predicate
            // proves progress; no observed duration is a timing acceptance.
            thread::park_timeout(Duration::from_millis(5));
        }
    });
    let (owner, [mut publisher, _second, _third]) = channel(0, wait(&thread_rx));
    let shared = owner.shared.clone();
    owner_tx.send(owner).unwrap();
    let epoch = wait(&epoch_rx);
    wait(&checked_rx);
    let mut audit = Audit::default();
    let payload = audit.payload(0, 0);
    let key = payload.key;
    let Claim::Accepted(permit) = publisher.reserve(epoch, payload) else {
        panic!("wake sample was rejected")
    };
    audit.accepted(key);
    match schedule {
        WakeSchedule::Normal => permit.commit(),
        WakeSchedule::Delayed => permit.commit_with(
            || {},
            || {
                assert_eq!(shared.wake_count(), 0);
                wait(&observed_rx);
                // Readiness/commit are already visible, but native notification
                // has not happened. The owner's bounded poll permits progress.
                assert_eq!(shared.wake_count(), 0);
            },
        ),
        WakeSchedule::Unwind => {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                permit.commit_with(
                    || {},
                    || {
                        assert_eq!(shared.wake_count(), 0);
                        // Intentional unwind without replacing the global panic
                        // hook or suppressing unrelated watchdog diagnostics.
                        std::panic::resume_unwind(Box::new("fixture after-ready unwind"));
                    },
                );
            }));
            assert!(result.is_err());
        }
    }
    assert_eq!(shared.wake_count(), 1);
    let (mut owner, payloads) = worker.join().unwrap();
    assert_eq!(owner.obligations(), (0, 0));
    audit.received(payloads, false);
    owner.cancel();
    completed(&mut owner, Status::Cancelled);
    report(
        match schedule {
            WakeSchedule::Normal => "host-owner-poll-normal-publication",
            WakeSchedule::Delayed => "host-ready-before-delayed-unpark",
            WakeSchedule::Unwind => "host-retirement-wake-survives-unwind",
        },
        &audit,
        &[],
    );
}

pub fn run() {
    serial("pooled-nominal-404", nominal(200));
    serial("pooled-nominal-606", nominal(300));
    serial(
        "pooled-single-source-400",
        (0..400)
            .map(|sequence| Key {
                source: 0,
                sequence,
            })
            .collect(),
    );
    serial(
        "pooled-gnss-burst-600",
        (0..600)
            .map(|sequence| Key {
                source: 2,
                sequence,
            })
            .collect(),
    );
    serial(
        "pooled-uneven-bursts-520",
        [(0, 300), (1, 180), (2, 40)]
            .into_iter()
            .flat_map(|(source, count)| (0..count).map(move |sequence| Key { source, sequence }))
            .collect(),
    );
    paused_admission(false);
    paused_admission(true);
    simultaneous_overflow(512);
    simultaneous_overflow(511);
    paused_marker();
    returned_credit_before_close();
    cancelled_restart();
    close_between_snapshot_and_admission();
    abandoned_permits(false);
    abandoned_permits(true);
    forgotten_permit();
    published_unwind_after_dequeue();
    epoch_exhaustion();
    startup_rollback();
    concurrent_source_fifo();
    host_wake(WakeSchedule::Normal);
    host_wake(WakeSchedule::Delayed);
    host_wake(WakeSchedule::Unwind);
}
