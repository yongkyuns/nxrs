//! Host controls for the actual scalar pool; scheduling hooks are test-only.
#![forbid(unsafe_code)]

use super::capture_pool::{
    self as pool, CAPACITY, Captured, Key, Record, Rejection, Stage, Status,
};
use allocation_probe::Snapshot;
use std::panic::{AssertUnwindSafe, catch_unwind, resume_unwind};
use std::sync::{Barrier, mpsc};
use std::thread;
use std::time::{Duration, Instant};

fn record(source: u8, sequence: u32, generation: u32) -> Record {
    Record {
        time_ms: (u64::from(0x8123_0000 | generation) << 32)
            | u64::from(sequence.wrapping_mul(101).wrapping_add(u32::from(source))),
        sequence,
        values: [
            0,
            u32::MAX,
            0x7fc0_1234 ^ generation,
            sequence.rotate_left(7) ^ u32::from(source),
            sequence.wrapping_mul(0x8123_4567) ^ generation,
            u32::from(source) << 24 | generation,
        ],
    }
}

fn report(case: &str, admitted: usize, delivered: usize, discarded: usize) {
    assert_eq!(admitted, delivered + discarded);
    println!(
        "CAPTURE_POOL {{\"case\":\"{case}\",\"runtime\":\"host\",\"shared_capacity\":512,\"admitted\":{admitted},\"delivered\":{delivered},\"discarded\":{discarded},\"production_migrated\":false,\"native_progress_qualified\":false,\"success\":true}}"
    );
}

fn finish(owner: &mut pool::Owner, expected: Status) {
    assert_eq!(owner.status(), expected);
    assert_eq!(owner.status(), expected);
    assert_eq!(owner.begin(), Err(pool::StartError::Pending));
    assert_eq!(owner.finish(), Ok(expected));
    assert_eq!(owner.status(), Status::Inactive);
    assert_eq!(owner.obligations(), (0, 0));
    assert_eq!(owner.free_slots(), 0);
}

fn workload(case: &str, offers: &[(u8, Record)]) {
    let (mut owner, mut publishers) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    let admitted = offers.len().min(CAPACITY);
    let mut expected: [Vec<Record>; 3] = std::array::from_fn(|_| Vec::new());
    for (index, &(source, value)) in offers.iter().enumerate() {
        let result = publishers[usize::from(source)].offer(epoch, value);
        if index < CAPACITY {
            assert_eq!(result, Ok(()));
            expected[usize::from(source)].push(value);
        } else {
            let rejected = result.unwrap_err();
            assert_eq!(rejected.record, value);
            assert_eq!(
                rejected.reason,
                if index == CAPACITY {
                    Rejection::Full
                } else {
                    Rejection::Closed
                }
            );
        }
    }
    let mut actual: [Vec<Record>; 3] = std::array::from_fn(|_| Vec::new());
    while let Some(value) = owner.poll() {
        actual[usize::from(value.source)].push(value.record);
    }
    assert_eq!(
        actual, expected,
        "missing, duplicate, reordered, or torn record"
    );
    if let Some(&(source, value)) = offers.get(CAPACITY) {
        finish(
            &mut owner,
            Status::Overflow(Key {
                source,
                sequence: value.sequence,
            }),
        );
    } else {
        owner.cancel();
        finish(&mut owner, Status::Cancelled);
    }
    report(case, admitted, admitted, 0);
}

fn nominal(ticks: u32) -> Vec<(u8, Record)> {
    let mut offers = Vec::new();
    for tick in 0..ticks {
        offers.push((0, record(0, tick, 0)));
        offers.push((1, record(1, tick, 0)));
        if tick % 50 == 0 {
            offers.push((2, record(2, tick / 50, 0)));
        }
    }
    offers
}

fn wrap_reuse() {
    let (mut owner, mut publishers) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    // A single source may occupy all slots, then reuse every lane index and
    // physical slot. Sources rotate so each full-capacity lane wraps twice.
    for round in 0..6_u32 {
        let source = (round % 3) as u8;
        for sequence in 0..CAPACITY as u32 {
            publishers[usize::from(source)]
                .offer(epoch, record(source, sequence, round))
                .unwrap();
        }
        assert_eq!(owner.free_slots(), 0);
        for sequence in 0..CAPACITY as u32 {
            assert_eq!(
                owner.poll(),
                Some(Captured {
                    source,
                    record: record(source, sequence, round),
                })
            );
        }
        assert_eq!(owner.poll(), None);
        assert_eq!(owner.free_slots(), CAPACITY);
        assert_eq!(owner.obligations(), (0, 0));
    }
    owner.cancel();
    finish(&mut owner, Status::Cancelled);
    report(
        "all-source-full-lane-wrap-reuse",
        6 * CAPACITY,
        6 * CAPACITY,
        0,
    );
}

fn pause_inside_transport(stage: Stage) {
    let (mut owner, [mut low, mut other, mut overflow]) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    let low_record = record(0, 41, 0);
    let (paused_tx, paused_rx) = mpsc::sync_channel(0);
    let (resume_tx, resume_rx) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let low = &mut low;
        let producer = scope.spawn(move || {
            low.offer_with_hooks(epoch, low_record, |at| {
                if at == stage {
                    paused_tx.send(()).unwrap();
                    resume_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                }
            })
            .unwrap();
        });
        paused_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(owner.free_slots(), CAPACITY - 1);
        assert_eq!(owner.poll(), None, "unpublished fields must stay invisible");
        for sequence in 0..511 {
            other.offer(epoch, record(1, sequence, 0)).unwrap();
        }
        let missing = record(2, 73, 0);
        assert_eq!(
            overflow.offer(epoch, missing),
            Err(pool::Rejected {
                reason: Rejection::Full,
                record: missing,
            })
        );
        assert_eq!(owner.free_slots(), 0);
        for sequence in 0..511 {
            assert_eq!(
                owner.poll(),
                Some(Captured {
                    source: 1,
                    record: record(1, sequence, 0),
                })
            );
        }
        assert_eq!(owner.poll(), None);
        assert_eq!(owner.obligations().0, 1);
        assert_ne!(owner.obligations().1, 0);
        assert_eq!(owner.status(), Status::Pending);
        assert_eq!(owner.begin(), Err(pool::StartError::Pending));
        assert_eq!(owner.finish(), Err(pool::StartError::Pending));
        resume_tx.send(()).unwrap();
        producer.join().unwrap();
    });
    assert_eq!(
        owner.poll(),
        Some(Captured {
            source: 0,
            record: low_record,
        })
    );
    finish(
        &mut owner,
        Status::Overflow(Key {
            source: 2,
            sequence: 73,
        }),
    );
    let next = owner.begin().unwrap();
    assert_ne!(next, epoch);
    let fresh = record(0, 41, 1); // Same source/sequence, different original bits.
    assert_eq!(
        low.offer(epoch, low_record).unwrap_err().reason,
        Rejection::Closed
    );
    low.offer(next, fresh).unwrap();
    assert_eq!(
        owner.poll(),
        Some(Captured {
            source: 0,
            record: fresh
        })
    );
    assert_eq!(owner.poll(), None);
    owner.cancel();
    finish(&mut owner, Status::Cancelled);
    report(
        match stage {
            Stage::AfterSlotClaim => "paused-slot-claim-other-sources-progress",
            Stage::MidPayload => "paused-mid-payload-other-sources-progress",
            Stage::BeforePublication => "paused-before-cursor-other-sources-progress",
            Stage::AfterPublication => unreachable!(),
        },
        CAPACITY + 1,
        CAPACITY + 1,
        0,
    );
}

fn unpublished_unwind(stage: Stage) {
    let (mut owner, mut publishers) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    let result = catch_unwind(AssertUnwindSafe(|| {
        publishers[0]
            .offer_with_hooks(epoch, record(0, 7, 0), |at| {
                if at == stage {
                    resume_unwind(Box::new("scheduled unpublished unwind"));
                }
            })
            .unwrap();
    }));
    assert!(result.is_err());
    assert_eq!(owner.poll(), None);
    finish(&mut owner, Status::Cancelled);
    let next = owner.begin().unwrap();
    publishers[0].offer(next, record(0, 7, 1)).unwrap();
    assert_eq!(
        owner.poll(),
        Some(Captured {
            source: 0,
            record: record(0, 7, 1),
        })
    );
    owner.cancel();
    finish(&mut owner, Status::Cancelled);
    report(
        match stage {
            Stage::AfterSlotClaim => "unwind-after-slot-claim-returns-storage",
            Stage::MidPayload => "unwind-mid-payload-returns-storage",
            Stage::BeforePublication => "unwind-before-cursor-returns-storage",
            Stage::AfterPublication => unreachable!(),
        },
        2,
        1,
        1,
    );
}

fn published_unwind() {
    let (mut owner, [mut publisher, _other, _last]) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    let (published_tx, published_rx) = mpsc::sync_channel(0);
    let (resume_tx, resume_rx) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let publisher = &mut publisher;
        let producer = scope.spawn(move || {
            catch_unwind(AssertUnwindSafe(|| {
                publisher
                    .offer_with_hooks(epoch, record(0, 18, 0), |stage| {
                        if stage == Stage::AfterPublication {
                            published_tx.send(()).unwrap();
                            resume_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                            resume_unwind(Box::new("scheduled published unwind"));
                        }
                    })
                    .unwrap();
            }))
        });
        published_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(
            owner.poll(),
            Some(Captured {
                source: 0,
                record: record(0, 18, 0),
            })
        );
        assert_eq!(owner.obligations().0, 0);
        assert_ne!(owner.obligations().1, 0);
        assert_eq!(owner.free_slots(), CAPACITY);
        owner.cancel();
        assert_eq!(owner.status(), Status::Pending);
        resume_tx.send(()).unwrap();
        assert!(producer.join().unwrap().is_err());
    });
    finish(&mut owner, Status::Cancelled);
    report("published-unwind-after-dequeue-no-double-return", 1, 1, 0);
}

fn cancellation_with_paused_storage() {
    let (mut owner, [mut paused, mut other, _last]) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    let (paused_tx, paused_rx) = mpsc::sync_channel(0);
    let (resume_tx, resume_rx) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let paused = &mut paused;
        let producer = scope.spawn(move || {
            paused
                .offer_with_hooks(epoch, record(0, 17, 0), |stage| {
                    if stage == Stage::AfterSlotClaim {
                        paused_tx.send(()).unwrap();
                        resume_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                    }
                })
                .unwrap();
        });
        paused_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        other.offer(epoch, record(1, 18, 0)).unwrap();
        owner.cancel();
        assert_eq!(owner.status(), Status::Pending);
        assert_eq!(owner.begin(), Err(pool::StartError::Pending));
        assert_eq!(
            owner.poll(),
            Some(Captured {
                source: 1,
                record: record(1, 18, 0)
            })
        ); // The owner accounts this cancelled epoch's record as discarded.
        assert_eq!(owner.poll(), None);
        assert_eq!(owner.free_slots(), CAPACITY - 1);
        assert_eq!(owner.status(), Status::Pending);
        resume_tx.send(()).unwrap();
        producer.join().unwrap();
    });
    assert_eq!(
        owner.poll(),
        Some(Captured {
            source: 0,
            record: record(0, 17, 0)
        })
    );
    finish(&mut owner, Status::Cancelled);
    let next = owner.begin().unwrap();
    assert_eq!(
        other.offer(epoch, record(1, 18, 0)).unwrap_err().reason,
        Rejection::Closed
    );
    paused.offer(next, record(0, 17, 1)).unwrap();
    other.offer(next, record(1, 18, 1)).unwrap();
    let mut delivered = [false; 2];
    for _ in 0..2 {
        let value = owner.poll().unwrap();
        assert!(value.source < 2 && !delivered[usize::from(value.source)]);
        assert_eq!(
            value.record,
            record(value.source, if value.source == 0 { 17 } else { 18 }, 1)
        );
        delivered[usize::from(value.source)] = true;
    }
    assert_eq!(delivered, [true; 2]);
    assert_eq!(owner.poll(), None);
    owner.cancel();
    finish(&mut owner, Status::Cancelled);
    report(
        "cancel-paused-storage-before-reused-identity-restart",
        4,
        2,
        2,
    );
}

fn owner_drop_after_open() {
    let (mut owner, mut publishers) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    publishers[0].offer(epoch, record(0, 0, 0)).unwrap();
    drop(owner);
    for (source, publisher) in publishers.iter_mut().enumerate() {
        let value = record(source as u8, 1, 0);
        assert_eq!(
            publisher.offer(epoch, value),
            Err(pool::Rejected {
                reason: Rejection::Closed,
                record: value
            })
        );
    }
    drop(publishers); // No consumer remains; the queued scalar is discarded.
    report("owner-drop-closes-admission", 1, 0, 1);
}

fn owner_drop_during_publication() {
    let (mut owner, [mut paused, mut other, last]) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    let (paused_tx, paused_rx) = mpsc::sync_channel(0);
    let (resume_tx, resume_rx) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let paused = &mut paused;
        let producer = scope.spawn(move || {
            paused
                .offer_with_hooks(epoch, record(0, 0, 0), |stage| {
                    if stage == Stage::AfterSlotClaim {
                        paused_tx.send(()).unwrap();
                        resume_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                    }
                })
                .unwrap();
        });
        paused_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        drop(owner);
        let value = record(1, 0, 0);
        assert_eq!(
            other.offer(epoch, value),
            Err(pool::Rejected {
                reason: Rejection::Closed,
                record: value
            })
        );
        resume_tx.send(()).unwrap();
        producer.join().unwrap();
    });
    assert_eq!(
        paused.offer(epoch, record(0, 1, 0)).unwrap_err().reason,
        Rejection::Closed
    );
    drop((paused, other, last)); // The last Arc outlives the admitted publication.
    report("owner-drop-retains-paused-publication-storage", 1, 0, 1);
}

fn last_publisher_drop() {
    let (mut owner, mut publishers) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    publishers[0].offer(epoch, record(0, 0, 0)).unwrap();
    assert!(owner.take_ready());
    drop(publishers);
    assert!(
        owner.take_ready(),
        "last source retirement must notify cancellation"
    );
    assert_eq!(owner.status(), Status::Pending);
    assert_eq!(
        owner.poll(),
        Some(Captured {
            source: 0,
            record: record(0, 0, 0)
        })
    );
    finish(&mut owner, Status::Cancelled);
    assert_eq!(owner.begin(), Err(pool::StartError::Disconnected));
    report("last-source-drop-cancels-and-notifies", 1, 0, 1);
}

fn last_publisher_drop_after_overflow() {
    let (mut owner, mut publishers) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    for sequence in 0..CAPACITY as u32 {
        publishers[0].offer(epoch, record(0, sequence, 0)).unwrap();
    }
    assert_eq!(
        publishers[1]
            .offer(epoch, record(1, 73, 0))
            .unwrap_err()
            .reason,
        Rejection::Full
    );
    drop(publishers);
    for sequence in 0..CAPACITY as u32 {
        assert_eq!(
            owner.poll(),
            Some(Captured {
                source: 0,
                record: record(0, sequence, 0)
            })
        );
    }
    finish(
        &mut owner,
        Status::Overflow(Key {
            source: 1,
            sequence: 73,
        }),
    );
    assert_eq!(owner.begin(), Err(pool::StartError::Disconnected));
    report(
        "last-source-drop-retains-overflow-terminal",
        CAPACITY,
        CAPACITY,
        0,
    );
}

fn source_drop_during_open(stage: pool::OpenStage, publish: bool) {
    let (mut owner, publishers) = pool::channel(thread::current());
    let mut publishers = Some(publishers);
    let opened = owner.begin_with_hooks(|at| {
        if at == stage {
            if publish {
                assert_eq!(stage, pool::OpenStage::AfterOpen);
                let publisher = &mut publishers.as_mut().unwrap()[0];
                publisher
                    .offer(publisher.captured_epoch(), record(0, 7, 0))
                    .unwrap();
            }
            drop(publishers.take());
        }
    });
    assert!(publishers.is_none());
    if publish {
        assert!(
            opened.is_ok(),
            "accepted work must remain observable for accounting"
        );
        assert_eq!(owner.status(), Status::Pending);
        assert_eq!(
            owner.poll(),
            Some(Captured {
                source: 0,
                record: record(0, 7, 0)
            })
        );
        finish(&mut owner, Status::Cancelled);
    } else {
        assert_eq!(opened, Err(pool::StartError::Disconnected));
        assert_eq!(owner.status(), Status::Inactive);
        assert_eq!(owner.obligations(), (0, 0));
        assert_eq!(owner.free_slots(), 0);
    }
    assert_eq!(owner.begin(), Err(pool::StartError::Disconnected));
    report(
        match (stage, publish) {
            (pool::OpenStage::BeforeOpen, false) => "last-source-drop-before-open-cas",
            (pool::OpenStage::AfterOpen, false) => "last-source-drop-after-open-empty-rollback",
            (pool::OpenStage::AfterOpen, true) => {
                "last-source-drop-after-open-retains-admitted-work"
            }
            (pool::OpenStage::BeforeOpen, true) => unreachable!(),
        },
        usize::from(publish),
        0,
        usize::from(publish),
    );
}

fn concurrent_complete_records() {
    const ROUNDS: u32 = 16;
    const BATCH: u32 = 64;
    let (mut owner, publishers) = pool::channel(thread::current());
    let epoch = owner.begin().unwrap();
    let boundary = Barrier::new(4);
    thread::scope(|scope| {
        for (source, mut publisher) in publishers.into_iter().enumerate() {
            let boundary = &boundary;
            scope.spawn(move || {
                for round in 0..ROUNDS {
                    boundary.wait();
                    for offset in 0..BATCH {
                        let sequence = round * BATCH + offset;
                        publisher
                            .offer(epoch, record(source as u8, sequence, 0))
                            .unwrap();
                    }
                    boundary.wait();
                }
            });
        }
        let mut expected = [0_u32; 3];
        for _ in 0..ROUNDS {
            boundary.wait();
            let deadline = Instant::now() + Duration::from_secs(5);
            let mut count = 0;
            while count < 3 * BATCH {
                if let Some(value) = owner.poll() {
                    let source = usize::from(value.source);
                    assert_eq!(value.record, record(value.source, expected[source], 0));
                    expected[source] += 1;
                    count += 1;
                } else {
                    assert!(Instant::now() < deadline, "concurrent publication stalled");
                    thread::yield_now();
                }
            }
            boundary.wait();
            assert_eq!(owner.poll(), None);
            assert_eq!(owner.obligations(), (0, 0));
            assert_eq!(owner.free_slots(), CAPACITY);
        }
        assert_eq!(expected, [ROUNDS * BATCH; 3]);
    });
    owner.cancel();
    finish(&mut owner, Status::Cancelled);
    assert_eq!(owner.begin(), Err(pool::StartError::Disconnected));
    report(
        "concurrent-complete-records-and-per-source-fifo",
        (ROUNDS * BATCH * 3) as usize,
        (ROUNDS * BATCH * 3) as usize,
        0,
    );
}

/// Invoke before starting the parent fixture's watchdog or other threads.
pub fn allocation_windows(snapshot: impl Fn() -> Snapshot) {
    let wake = thread::current();
    let before = snapshot();
    let (mut owner, mut publishers) = pool::channel(wake);
    let setup = snapshot();
    setup.validate_after(before).unwrap();
    assert_eq!(owner.free_slots(), 0);
    let epoch = owner.begin().unwrap();
    let active = snapshot();
    let storage = pool::storage_report();
    active.validate_after(setup).unwrap();
    assert_eq!(
        active.live - setup.live,
        storage.payload_heap_bytes + storage.index_heap_bytes
    );
    assert_eq!(active.blocks - setup.blocks, 4);
    publishers[0].offer(epoch, record(0, 0, 0)).unwrap();
    assert_eq!(
        owner.poll(),
        Some(Captured {
            source: 0,
            record: record(0, 0, 0)
        })
    );
    let first = snapshot();
    first.no_allocator_calls_since(active).unwrap();
    for sequence in 1..4097_u32 {
        let source = (sequence % 3) as u8;
        publishers[usize::from(source)]
            .offer(epoch, record(source, sequence, 0))
            .unwrap();
        assert_eq!(
            owner.poll(),
            Some(Captured {
                source,
                record: record(source, sequence, 0)
            })
        );
    }
    let steady = snapshot();
    steady.no_allocator_calls_since(first).unwrap();
    owner.cancel();
    finish(&mut owner, Status::Cancelled);
    let inactive = snapshot();
    inactive.reclaimed_since(setup).unwrap();
    assert_eq!(inactive.dealloc - steady.dealloc, 4);
    drop(publishers);
    drop(owner);
    let teardown = snapshot();
    teardown.reclaimed_since(before).unwrap();
    println!(
        "CAPTURE_POOL_MEMORY {{\"runtime\":\"host\",\"payload_heap_bytes\":{},\"index_heap_bytes\":{},\"bitmap_bytes\":{},\"cursor_bytes\":{},\"storage_inline_bytes\":{},\"setup_requested_bytes\":{},\"setup_allocations\":{},\"active_requested_bytes\":{},\"first_use_allocator_calls\":0,\"steady_allocator_calls\":0,\"inactive_retained_bytes\":{},\"normal_teardown_reclaimed\":true,\"success\":true}}",
        storage.payload_heap_bytes,
        storage.index_heap_bytes,
        storage.bitmap_bytes,
        storage.cursor_bytes,
        storage.storage_inline_bytes,
        setup.live - before.live,
        setup.alloc - before.alloc,
        active.live - before.live,
        inactive.live - before.live,
    );
}

pub fn run() {
    workload("pooled-nominal-404", &nominal(200));
    workload("pooled-nominal-606", &nominal(300));
    workload(
        "pooled-single-source-400",
        &(0..400).map(|n| (0, record(0, n, 0))).collect::<Vec<_>>(),
    );
    workload(
        "pooled-gnss-burst-600",
        &(0..600).map(|n| (2, record(2, n, 0))).collect::<Vec<_>>(),
    );
    workload(
        "pooled-uneven-bursts-520",
        &(0..300)
            .map(|n| (0, record(0, n, 0)))
            .chain((0..200).map(|n| (1, record(1, n, 0))))
            .chain((0..20).map(|n| (2, record(2, n, 0))))
            .collect::<Vec<_>>(),
    );
    wrap_reuse();
    for stage in [
        Stage::AfterSlotClaim,
        Stage::MidPayload,
        Stage::BeforePublication,
    ] {
        pause_inside_transport(stage);
        unpublished_unwind(stage);
    }
    published_unwind();
    cancellation_with_paused_storage();
    owner_drop_after_open();
    owner_drop_during_publication();
    last_publisher_drop();
    last_publisher_drop_after_overflow();
    source_drop_during_open(pool::OpenStage::BeforeOpen, false);
    source_drop_during_open(pool::OpenStage::AfterOpen, false);
    source_drop_during_open(pool::OpenStage::AfterOpen, true);
    concurrent_complete_records();
}
