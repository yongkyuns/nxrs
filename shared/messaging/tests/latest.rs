//! Public latest-value contracts. Thread gates pause at API boundaries; the
//! raw-backend qualification fixture separately observes unfinished publication.
#![cfg(feature = "latest")]
#![forbid(unsafe_code)]

use messaging::latest::channel;
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Barrier,
    },
    thread,
    time::Duration,
};

const DEADLINE: Duration = Duration::from_secs(5);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Record {
    sequence: u64,
    complement: u64,
    words: [u64; 32],
}

impl Record {
    fn new(sequence: u64) -> Self {
        Self {
            sequence,
            complement: !sequence,
            words: core::array::from_fn(|index| sequence ^ (index as u64).rotate_left(17)),
        }
    }

    fn assert_complete(self) {
        assert_eq!(self, Self::new(self.sequence));
    }
}

#[test]
fn initial_and_published_records_are_independent_copies() {
    let mut initial = Record::new(0);
    let (mut publisher, [mut reader]) = channel::<_, 1>(initial);
    initial.words[0] = 99;
    assert_eq!(reader.read(), Record::new(0));

    let mut submitted = Record::new(1);
    publisher.publish(submitted);
    submitted.words[1] = 99;
    let retained = reader.read();
    assert_eq!(retained, Record::new(1));

    // Reading returns an owned copy, with no buffer borrow retained by callers.
    let mut edited = reader.read();
    edited.words[2] = 99;
    assert_eq!(reader.read(), Record::new(1));
    publisher.publish(Record::new(2));
    publisher.publish(Record::new(3));
    assert_eq!(retained, Record::new(1));
    assert_eq!(reader.read(), Record::new(3));
    assert_eq!(reader.read(), Record::new(3));
    assert_eq!(
        (initial.words[0], submitted.words[1], edited.words[2]),
        (99, 99, 99)
    );
}

#[test]
fn setup_publication_and_read_use_copy_without_calling_user_clone() {
    #[derive(Copy, Debug, PartialEq, Eq)]
    struct CopyOnly(u32);

    #[allow(
        clippy::non_canonical_clone_impl,
        reason = "the witness must panic if publication calls Clone instead of Copy"
    )]
    impl Clone for CopyOnly {
        fn clone(&self) -> Self {
            panic!("the Copy publication contract must not call user Clone");
        }
    }

    let (mut publisher, [mut first, mut second]) = channel::<_, 2>(CopyOnly(4));
    assert_eq!(first.read(), CopyOnly(4));
    publisher.publish(CopyOnly(5));
    assert_eq!(first.read(), CopyOnly(5));
    assert_eq!(second.read(), CopyOnly(5));
}

#[test]
fn readers_coalesce_independently_and_dropping_one_does_not_close_others() {
    let (mut publisher, [mut fast, mut slow, abandoned]) = channel::<_, 3>(0_u32);
    drop(abandoned);
    publisher.publish(1);
    assert_eq!(fast.read(), 1);
    publisher.publish(2);
    assert_eq!(fast.read(), 2);
    publisher.publish(3);
    assert_eq!(slow.read(), 3);
    assert_eq!(fast.read(), 3);
    drop(fast);
    publisher.publish(4);
    assert_eq!(slow.read(), 4);
    drop(slow);
    publisher.publish(5); // Endpoint teardown is not a publication error.
}

#[test]
fn publisher_drop_retains_initial_or_final_value_without_a_close_event() {
    let (publisher, [mut initial]) = channel::<_, 1>(Record::new(0));
    drop(publisher);
    assert_eq!(initial.read(), Record::new(0));

    let (mut publisher, [mut first, mut second]) = channel::<_, 2>(Record::new(0));
    publisher.publish(Record::new(9));
    assert_eq!(first.read(), Record::new(9));
    drop(publisher);
    assert_eq!(first.read(), Record::new(9));
    assert_eq!(second.read(), Record::new(9));
}

#[test]
fn reader_progresses_while_publisher_is_parked_between_publications() {
    let (mut publisher, [mut reader]) = channel::<_, 1>(Record::new(0));
    let (ready_tx, ready_rx) = mpsc::sync_channel(0);
    let (resume_tx, resume_rx) = mpsc::sync_channel(0);
    let producer = thread::spawn(move || {
        publisher.publish(Record::new(1));
        ready_tx.send(()).unwrap();
        // This is a fixture gate, not a wait inside the publication operation.
        resume_rx.recv_timeout(DEADLINE).unwrap();
        publisher.publish(Record::new(2));
    });
    ready_rx.recv_timeout(DEADLINE).unwrap();
    for _ in 0..100 {
        assert_eq!(reader.read(), Record::new(1));
    }
    resume_tx.send(()).unwrap();
    producer.join().unwrap();
    assert_eq!(reader.read(), Record::new(2));
}

#[test]
fn publisher_and_other_reader_progress_while_one_reader_retains_a_copy() {
    let (mut publisher, [mut parked, mut active]) = channel::<_, 2>(Record::new(0));
    publisher.publish(Record::new(1));
    let (ready_tx, ready_rx) = mpsc::sync_channel(0);
    let (resume_tx, resume_rx) = mpsc::sync_channel(0);
    let consumer = thread::spawn(move || {
        let retained = parked.read();
        ready_tx.send(()).unwrap();
        resume_rx.recv_timeout(DEADLINE).unwrap();
        assert_eq!(retained, Record::new(1));
        assert_eq!(parked.read(), Record::new(1_000));
    });
    ready_rx.recv_timeout(DEADLINE).unwrap();
    for sequence in 2..=1_000 {
        publisher.publish(Record::new(sequence));
        assert_eq!(active.read(), Record::new(sequence));
    }
    resume_tx.send(()).unwrap();
    consumer.join().unwrap();
}

#[test]
fn concurrent_readers_observe_whole_records_and_the_final_publication() {
    const LAST: u64 = 20_000;
    let (mut publisher, readers) = channel::<_, 2>(Record::new(0));
    let start = Arc::new(Barrier::new(3));
    let done = Arc::new(AtomicBool::new(false));
    let consumers: Vec<_> = readers
        .into_iter()
        .map(|mut reader| {
            let start = start.clone();
            let done = done.clone();
            thread::spawn(move || {
                assert_eq!(reader.read(), Record::new(0));
                start.wait();
                let mut previous = 0;
                while !done.load(Ordering::Acquire) {
                    let record = reader.read();
                    record.assert_complete();
                    assert!((previous..=LAST).contains(&record.sequence));
                    previous = record.sequence;
                    thread::yield_now();
                }
                assert_eq!(reader.read(), Record::new(LAST));
            })
        })
        .collect();
    start.wait();
    for sequence in 1..=LAST {
        publisher.publish(Record::new(sequence));
        if sequence % 64 == 0 {
            thread::yield_now();
        }
    }
    done.store(true, Ordering::Release);
    for consumer in consumers {
        consumer.join().unwrap();
    }
}

#[test]
#[should_panic(expected = "latest channel requires at least one reader")]
fn construction_rejects_missing_consumers() {
    let _ = channel::<_, 0>(0_u32);
}
