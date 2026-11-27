//! Host contract comparison, not a target timing benchmark or capture replacement.
#![cfg(not(any(target_os = "nuttx", target_os = "none")))]

#[rustfmt::skip]
#[allow(dead_code, unused_imports)]
#[path = "../../../../app/motion-tracker/src/communications/export.rs"]
mod capture;
#[rustfmt::skip]
#[allow(dead_code)]
#[path = "../../../../app/motion-tracker/src/clock.rs"]
mod clock;

#[path = "support/capture_protocol.rs"]
mod capture_protocol;

use allocation_probe::{Snapshot, Tracking};
use capture::{Capture, MAX_BYTES, Version};
use heapless::spsc::Queue;
use navigation::observations::Measurement;
use std::alloc::System;
use std::io::Write;
use std::mem::size_of;
use std::process::{Command, Stdio};
use std::sync::{
    Barrier,
    atomic::{AtomicUsize, Ordering},
    mpsc::{self, Receiver, SyncSender, TrySendError},
};
use std::thread;
use std::time::Duration;

#[global_allocator]
static ALLOCATOR: Tracking<System> = Tracking::new(System);

const CAPACITY: usize = 512;
const BATCH: usize = 16;

// The fields retained by capture, independently reconstructed from its public
// wire format. This is not an alias or a layout assumption about private Sample.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Record {
    time_ms: u64,
    sequence: u32,
    source: u8,
    values: [u32; 6],
}

impl Record {
    fn new(source: u8, sequence: u32) -> Self {
        assert!((1..=3).contains(&source));
        let mut values = std::array::from_fn(|axis| {
            (f32::from(source) + sequence as f32 * 0.125 + axis as f32 * 0.25).to_bits()
        });
        if source == 3 {
            let latitude = (45.0_f64 + f64::from(sequence) * 0.00001).to_bits();
            let longitude = (-76.0_f64 - f64::from(sequence) * 0.00002).to_bits();
            values[..4].copy_from_slice(&[
                latitude as u32,
                (latitude >> 32) as u32,
                longitude as u32,
                (longitude >> 32) as u32,
            ]);
            values[5] = 0;
        }
        Self {
            time_ms: u64::from(sequence) * if source == 3 { 500 } else { 10 } + u64::from(source),
            sequence,
            source,
            values,
        }
    }

    fn measurement(self) -> Measurement {
        let timestamp_us = self.time_ms * 1_000 + 999;
        if self.source == 3 {
            Measurement::Gnss(gnss::GnssFix {
                sequence: u64::from(self.sequence),
                timestamp_us,
                is_capture_time: true,
                receiver_utc_ms: None,
                latitude_deg: f64::from_bits(
                    u64::from(self.values[0]) | u64::from(self.values[1]) << 32,
                ),
                longitude_deg: f64::from_bits(
                    u64::from(self.values[2]) | u64::from(self.values[3]) << 32,
                ),
                speed_mps: f32::from_bits(self.values[4]),
                course_rad: None,
                quality: None,
            })
        } else {
            Measurement::Imu(
                usize::from(self.source - 1),
                imu::ImuSample {
                    sequence: u64::from(self.sequence),
                    timestamp_us,
                    is_capture_time: true,
                    sample_interval_us: 10_000,
                    accel_mps2: <[u32; 3]>::try_from(&self.values[..3])
                        .unwrap()
                        .map(f32::from_bits),
                    gyro_rps: <[u32; 3]>::try_from(&self.values[3..])
                        .unwrap()
                        .map(f32::from_bits),
                },
            )
        }
    }

    fn identity(self) -> (u8, u32) {
        (self.source, self.sequence)
    }
}

// Independent decoder for the unchanged encoder's full-float v1/v2/v3 output.
// Native-count reconstruction, UTC anchoring and client/transport integration
// remain covered by their owning fixtures, not by these queue models.
fn decode(bytes: &[u8], records: &mut Vec<Record>) {
    assert_eq!(bytes[0], 0xa5);
    let version = bytes[1];
    assert!((1..=3).contains(&version));
    assert_eq!(
        usize::from(u16::from_le_bytes(bytes[2..4].try_into().unwrap())),
        bytes.len() - 4
    );
    let mut cursor = 4;
    if version == 3 {
        assert_eq!(&bytes[cursor..cursor + 8], &i64::MIN.to_le_bytes());
        cursor += 8;
    }
    let mut previous = [None::<(u32, u64)>; 3];
    while cursor < bytes.len() {
        let tag = bytes[cursor];
        cursor += 1;
        assert_eq!(tag & 0x60, 0);
        let source = tag & 0x1f;
        assert!((1..=3).contains(&source));
        let prior = &mut previous[usize::from(source - 1)];
        let (sequence, time_ms) = if tag & 0x80 != 0 {
            assert_ne!(version, 1);
            let (sequence, time) = prior.unwrap();
            let delta = bytes[cursor];
            cursor += 1;
            (sequence.wrapping_add(1), time + u64::from(delta))
        } else {
            let sequence = u32::from_le_bytes(bytes[cursor..cursor + 4].try_into().unwrap());
            cursor += 4;
            let time = if version == 3 {
                let value = u64::from_le_bytes(bytes[cursor..cursor + 8].try_into().unwrap());
                cursor += 8;
                value
            } else {
                let value = u32::from_le_bytes(bytes[cursor..cursor + 4].try_into().unwrap());
                cursor += 4;
                u64::from(value)
            };
            (sequence, time)
        };
        *prior = Some((sequence, time_ms));
        let mut values = [0; 6];
        for value in &mut values[..if source == 3 { 5 } else { 6 }] {
            *value = u32::from_le_bytes(bytes[cursor..cursor + 4].try_into().unwrap());
            cursor += 4;
        }
        records.push(Record {
            source,
            sequence,
            time_ms,
            values,
        });
    }
    assert_eq!(cursor, bytes.len());
}

fn drain_capture(capture: &Capture, records: &mut Vec<Record>) -> Option<(u8, u32)> {
    let mut bytes = [0; MAX_BYTES];
    for _ in 0..=CAPACITY / BATCH {
        let Some(batch) = capture.take(&mut bytes, true, Version::Fixed) else {
            return None;
        };
        if batch.terminal {
            assert_eq!(&bytes[..batch.len], &[0xa5, 1, 0, 0]);
            let terminal = capture.take(&mut bytes, true, Version::Compact).unwrap();
            assert!(terminal.terminal);
            assert_eq!(terminal.len, 9);
            assert_eq!(&bytes[..4], &[0xa5, 2, 5, 0]);
            let missing = (
                bytes[4],
                u32::from_le_bytes(bytes[5..9].try_into().unwrap()),
            );
            let repeated = capture.take(&mut bytes, true, Version::Timed).unwrap();
            assert!(repeated.terminal);
            assert_eq!(repeated.len, 9);
            assert_eq!(missing.0, bytes[4]);
            assert_eq!(
                missing.1,
                u32::from_le_bytes(bytes[5..9].try_into().unwrap())
            );
            return Some(missing);
        }
        decode(&bytes[..batch.len], records);
    }
    panic!("capture did not finish its bounded drain");
}

struct ResultRow {
    backend: &'static str,
    capacities: Option<[usize; 3]>,
    delivered: Vec<Record>,
    missing: Option<(u8, u32)>,
    setup: Snapshot,
    ready: Snapshot,
    steady: Snapshot,
    retired: Snapshot,
    inline_bytes: usize,
}

impl ResultRow {
    fn verify(&self, offers: &[Record]) {
        self.steady.no_allocator_calls_since(self.ready).unwrap();
        self.retired.reclaimed_since(self.setup).unwrap();
        let admitted = self.delivered.len();
        assert!(admitted <= CAPACITY);
        assert_eq!(
            self.missing,
            offers.get(admitted).copied().map(Record::identity)
        );
        for source in 1..=3 {
            assert!(
                self.delivered
                    .iter()
                    .filter(|record| record.source == source)
                    .eq(offers[..admitted]
                        .iter()
                        .filter(|record| record.source == source)),
                "source {source} lost order or payload bits"
            );
        }
        if self.capacities.is_none() {
            assert_eq!(self.delivered, offers[..admitted]);
        }
    }

    fn report(&self, case: &str, offers: &[Record]) {
        self.verify(offers);
        let admitted = self.delivered.len();
        let full = usize::from(self.missing.is_some());
        let closed = offers.len() - admitted - full;
        let missing = self.missing.map_or_else(
            || "null".into(),
            |(source, sequence)| format!("{{\"source\":{source},\"sequence\":{sequence}}}"),
        );
        let partition = self
            .capacities
            .map_or_else(|| "null".into(), |value| format!("{value:?}"));
        let wire_bytes = [
            encoded_bytes(&self.delivered, Version::Fixed),
            encoded_bytes(&self.delivered, Version::Compact),
            encoded_bytes(&self.delivered, Version::Timed),
        ];
        println!(
            "CAPTURE_COMPARE {{\"case\":\"{case}\",\"backend\":\"{}\",\"usable_slots\":512,\"per_source_slots\":{partition},\"offered\":{},\"admitted\":{admitted},\"rejected_full\":{full},\"rejected_closed\":{closed},\"delivered\":{admitted},\"retained\":0,\"discarded\":0,\"first_missing\":{missing},\"unused_slots_at_overflow\":{},\"cross_source_reordered\":{},\"canonical_record_bytes\":{},\"inline_storage_bytes\":{},\"setup_requested_heap_bytes\":{},\"steady_allocator_calls\":0,\"reclaimed\":true,\"posthoc_wire_bytes_v1_v2_v3\":{wire_bytes:?}}}",
            self.backend,
            offers.len(),
            if full == 0 { 0 } else { CAPACITY - admitted },
            self.delivered != offers[..admitted],
            size_of::<Record>(),
            self.inline_bytes,
            self.ready.live - self.setup.live,
        );
    }
}

fn current_capture(offers: &[Record]) -> ResultRow {
    let mut delivered = Vec::with_capacity(CAPACITY);
    let capture = Capture::default();
    capture.register_owner();
    let setup = ALLOCATOR.snapshot();
    capture.enable().unwrap();
    let ready = ALLOCATOR.snapshot();
    for record in offers {
        capture.record(record.measurement());
    }
    let missing = drain_capture(&capture, &mut delivered);
    assert_eq!(capture.take_ready(), offers.len() >= BATCH);
    let steady = ALLOCATOR.snapshot();
    assert_eq!(capture.finish(), delivered.len());
    let retired = ALLOCATOR.snapshot();
    ResultRow {
        backend: "current-capture",
        capacities: None,
        delivered,
        missing,
        setup,
        ready,
        steady,
        retired,
        inline_bytes: size_of::<Capture>(),
    }
}

trait Fifo {
    fn try_send(&mut self, record: Record) -> Result<(), Record>;
    fn try_recv(&mut self) -> Option<Record>;
}

struct DesktopMpsc(SyncSender<Record>, Receiver<Record>);

impl Fifo for DesktopMpsc {
    fn try_send(&mut self, record: Record) -> Result<(), Record> {
        match self.0.try_send(record) {
            Ok(()) => Ok(()),
            Err(TrySendError::Full(record)) => Err(record),
            Err(TrySendError::Disconnected(_)) => panic!("fixture receiver disappeared"),
        }
    }

    fn try_recv(&mut self) -> Option<Record> {
        self.1.try_recv().ok()
    }
}

struct Partitioned<const A: usize, const B: usize, const C: usize> {
    first: Queue<Record, A>,
    second: Queue<Record, B>,
    gnss: Queue<Record, C>,
    next: usize,
}

impl<const A: usize, const B: usize, const C: usize> Partitioned<A, B, C> {
    fn new() -> Self {
        assert_eq!(A + B + C - 3, CAPACITY);
        Self {
            first: Queue::new(),
            second: Queue::new(),
            gnss: Queue::new(),
            next: 0,
        }
    }
}

impl<const A: usize, const B: usize, const C: usize> Fifo for Partitioned<A, B, C> {
    fn try_send(&mut self, record: Record) -> Result<(), Record> {
        match record.source {
            1 => self.first.enqueue(record),
            2 => self.second.enqueue(record),
            3 => self.gnss.enqueue(record),
            _ => unreachable!(),
        }
    }

    fn try_recv(&mut self) -> Option<Record> {
        for offset in 0..3 {
            let source = (self.next + offset) % 3;
            let record = match source {
                0 => self.first.dequeue(),
                1 => self.second.dequeue(),
                2 => self.gnss.dequeue(),
                _ => unreachable!(),
            };
            if record.is_some() {
                self.next = (source + 1) % 3;
                return record;
            }
        }
        None
    }
}

fn queue_model<Q: Fifo>(
    backend: &'static str,
    capacities: Option<[usize; 3]>,
    offers: &[Record],
    make: impl FnOnce() -> Q,
) -> ResultRow {
    let mut delivered = Vec::with_capacity(CAPACITY);
    let setup = ALLOCATOR.snapshot();
    let mut queue = make();
    let ready = ALLOCATOR.snapshot();
    let mut missing = None;
    for record in offers {
        // Serial oracle policy only. This does not solve concurrent epoch
        // admission, closure, outstanding commits or missing-record arbitration.
        if missing.is_none() {
            if let Err(returned) = queue.try_send(*record) {
                assert_eq!(returned, *record);
                missing = Some(returned.identity());
            }
        }
    }
    while let Some(record) = queue.try_recv() {
        delivered.push(record);
    }
    let steady = ALLOCATOR.snapshot();
    drop(queue);
    let retired = ALLOCATOR.snapshot();
    ResultRow {
        backend,
        capacities,
        delivered,
        missing,
        setup,
        ready,
        steady,
        retired,
        inline_bytes: size_of::<Q>(),
    }
}

// Feed the selected merge order through the real encoder, outside all measured
// allocation windows. This observes wire size, not an integrated replacement.
fn encoded_bytes(records: &[Record], version: Version) -> usize {
    let capture = Capture::default();
    capture.enable().unwrap();
    let mut decoded = Vec::with_capacity(records.len());
    let mut bytes = [0; MAX_BYTES];
    let mut total = 0;
    for batch in records.chunks(BATCH) {
        for record in batch {
            capture.record(record.measurement());
        }
        let frame = capture.take(&mut bytes, true, version).unwrap();
        assert!(!frame.terminal);
        total += frame.len;
        decode(&bytes[..frame.len], &mut decoded);
    }
    assert_eq!(decoded, records);
    assert!(capture.take(&mut bytes, true, version).is_none());
    capture.finish();
    total
}

fn compare(case: &str, offers: &[Record]) {
    let baseline = current_capture(offers);
    baseline.report(case, offers);
    let mpsc = queue_model("desktop-std-mpsc-model", None, offers, || {
        let (sender, receiver) = mpsc::sync_channel(CAPACITY);
        DesktopMpsc(sender, receiver)
    });
    mpsc.report(case, offers);
    assert_eq!(mpsc.delivered, baseline.delivered);
    assert_eq!(mpsc.missing, baseline.missing);
    queue_model(
        "heapless-spsc-even-model",
        Some([171, 171, 170]),
        offers,
        Partitioned::<172, 172, 171>::new,
    )
    .report(case, offers);
    queue_model(
        "heapless-spsc-rate-model",
        Some([254, 254, 4]),
        offers,
        Partitioned::<255, 255, 5>::new,
    )
    .report(case, offers);
}

fn nominal(ticks: u32) -> Vec<Record> {
    let mut records = Vec::new();
    for tick in 0..ticks {
        records.push(Record::new(1, tick));
        records.push(Record::new(2, tick));
        if tick % 50 == 0 {
            records.push(Record::new(3, tick / 50));
        }
    }
    records
}

fn capture_concurrent_overflow_and_restart() {
    let capture = Capture::default();
    capture.enable().unwrap();
    let prefix = nominal(300);
    for record in &prefix[..CAPACITY] {
        capture.record(record.measurement());
    }
    let rejected = [Record::new(1, 900), Record::new(2, 901)];
    let start = Barrier::new(3);
    thread::scope(|scope| {
        for record in rejected {
            let capture = &capture;
            let start = &start;
            scope.spawn(move || {
                start.wait();
                capture.record(record.measurement());
            });
        }
        start.wait();
    });
    capture.record(Record::new(3, 902).measurement());
    let mut delivered = Vec::with_capacity(CAPACITY);
    let missing = drain_capture(&capture, &mut delivered).unwrap();
    assert_eq!(delivered, prefix[..CAPACITY]);
    assert!(rejected.iter().any(|record| record.identity() == missing));
    assert_eq!(capture.finish(), CAPACITY);
    capture.enable().unwrap();
    capture.record(Record::new(3, 903).measurement());
    delivered.clear();
    assert_eq!(drain_capture(&capture, &mut delivered), None);
    assert_eq!(delivered, [Record::new(3, 903)]);
    capture.finish();
    println!(
        "CAPTURE_CHECK {{\"case\":\"current-capture-concurrent-overflow-restart\",\"admitted\":512,\"delivered\":512,\"retained_first_missing\":true,\"fresh_epoch_clean\":true,\"success\":true}}"
    );
}

fn spsc_ownership_and_disconnect() {
    struct Owned<'a> {
        id: usize,
        dropped: &'a [AtomicUsize; 13],
    }
    impl Drop for Owned<'_> {
        fn drop(&mut self) {
            self.dropped[self.id].fetch_add(1, Ordering::Relaxed);
        }
    }
    let dropped = std::array::from_fn(|_| AtomicUsize::new(0));
    {
        let mut queue = Queue::<Owned<'_>, 9>::new();
        let (mut producer, mut consumer) = queue.split();
        for id in 0..8 {
            assert!(
                producer
                    .enqueue(Owned {
                        id,
                        dropped: &dropped
                    })
                    .is_ok()
            );
        }
        let returned = producer
            .enqueue(Owned {
                id: 8,
                dropped: &dropped,
            })
            .err()
            .unwrap();
        assert_eq!(returned.id, 8);
        drop(returned);
        for id in 0..4 {
            assert_eq!(consumer.dequeue().unwrap().id, id);
        }
        for id in 9..13 {
            assert!(
                producer
                    .enqueue(Owned {
                        id,
                        dropped: &dropped
                    })
                    .is_ok()
            );
        }
        for id in 4..8 {
            assert_eq!(consumer.dequeue().unwrap().id, id);
        }
        // Four retained values are reclaimed when the stable queue is retired.
    }
    assert!(
        dropped
            .iter()
            .all(|count| count.load(Ordering::Relaxed) == 1)
    );
    let mut queue = Queue::<Record, 3>::new();
    let (mut producer, consumer) = queue.split();
    drop(consumer);
    assert!(producer.enqueue(Record::new(1, 0)).is_ok());
    let (sender, receiver) = mpsc::sync_channel(2);
    drop(receiver);
    assert!(matches!(
        sender.try_send(Record::new(1, 0)),
        Err(TrySendError::Disconnected(record)) if record == Record::new(1, 0)
    ));
    println!(
        "CAPTURE_CHECK {{\"case\":\"spsc-full-wrap-retirement\",\"owned_values\":13,\"exactly_once_drop\":true,\"raw_spsc_consumer_drop_closes\":false,\"std_receiver_drop_closes\":true,\"success\":true}}"
    );
}

fn spsc_concurrent_handoff() {
    let mut first = Queue::<Record, 17>::new();
    let mut second = Queue::<Record, 17>::new();
    let mut gnss = Queue::<Record, 17>::new();
    let (first_producer, first_consumer) = first.split();
    let (second_producer, second_consumer) = second.split();
    let (gnss_producer, gnss_consumer) = gnss.split();
    let mut consumers = [first_consumer, second_consumer, gnss_consumer];
    // This barrier orchestrates the host witness only. It is not a proposed
    // runtime, queue notification mechanism, or part of an allocation window.
    let phase = Barrier::new(4);
    thread::scope(|scope| {
        for (source, mut producer) in [
            (1, first_producer),
            (2, second_producer),
            (3, gnss_producer),
        ] {
            let phase = &phase;
            scope.spawn(move || {
                for round in 0..32 {
                    for offset in 0..16 {
                        assert!(
                            producer
                                .enqueue(Record::new(source, round * 16 + offset))
                                .is_ok()
                        );
                    }
                    let rejected = Record::new(source, round * 16 + 16);
                    assert_eq!(producer.enqueue(rejected), Err(rejected));
                    phase.wait();
                    phase.wait();
                }
            });
        }
        for round in 0..32 {
            phase.wait();
            for offset in 0..16 {
                for (source, consumer) in consumers.iter_mut().enumerate() {
                    assert_eq!(
                        consumer.dequeue(),
                        Some(Record::new(source as u8 + 1, round * 16 + offset))
                    );
                }
            }
            assert!(
                consumers
                    .iter_mut()
                    .all(|consumer| consumer.dequeue().is_none())
            );
            phase.wait();
        }
    });
    println!(
        "CAPTURE_CHECK {{\"case\":\"spsc-concurrent-stalled-consumer\",\"producers\":3,\"rounds\":32,\"admitted\":1536,\"delivered\":1536,\"full_returns\":96,\"success\":true}}"
    );
}

fn epoch_check_is_not_a_commit_barrier() {
    let mut queue = Queue::<(usize, Record), 3>::new();
    let (mut producer, mut consumer) = queue.split();
    let epoch = AtomicUsize::new(1);
    let (checked, observed) = mpsc::sync_channel(0);
    let (commit, resume) = mpsc::sync_channel(0);
    thread::scope(|scope| {
        let epoch = &epoch;
        let worker = scope.spawn(move || {
            let admitted_epoch = epoch.load(Ordering::Acquire);
            // Even an immediate pre-enqueue check has a preemption window.
            assert_eq!(epoch.load(Ordering::Acquire), admitted_epoch);
            checked.send(()).unwrap();
            resume.recv().unwrap();
            producer
                .enqueue((admitted_epoch, Record::new(1, 0)))
                .unwrap();
        });
        observed.recv().unwrap();
        epoch.store(2, Ordering::Release); // close
        assert_eq!(consumer.dequeue(), None);
        epoch.store(3, Ordering::Release); // restart, without producer quiescence
        commit.send(()).unwrap();
        worker.join().unwrap();
        let (old_epoch, record) = consumer.dequeue().unwrap();
        assert_eq!(old_epoch, 1);
        assert_eq!(record, Record::new(1, 0));
        assert_ne!(old_epoch, epoch.load(Ordering::Acquire));
    });
    println!(
        "CAPTURE_CHECK {{\"case\":\"negative-control-epoch-check-only\",\"empty_before_old_commit\":true,\"old_commit_after_restart\":true,\"safe_queue_storage_retained\":true,\"success\":true}}"
    );
}

fn blob(bytes: &[u8]) -> String {
    let mut child = Command::new("git")
        .args(["hash-object", "--stdin"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("git is required for source provenance");
    child.stdin.take().unwrap().write_all(bytes).unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    let value = String::from_utf8(output.stdout).unwrap();
    let value = value.trim();
    assert!(value.len() == 40 && value.bytes().all(|byte| byte.is_ascii_hexdigit()));
    value.into()
}

fn main() {
    allocation_probe::begin_measurement();
    println!(
        "CAPTURE_METADATA {{\"schema\":\"capture-comparison-v1\",\"runtime\":\"host\",\"timing\":\"not-measured\",\"std_backend\":\"desktop-standard-library\",\"heapless_version\":\"0.9.4\",\"heapless_features\":[],\"source_hash\":\"git-blob-sha1\",\"capture_blob\":\"{}\",\"clock_blob\":\"{}\",\"fixture_blob\":\"{}\",\"protocol_blob\":\"{}\",\"manifest_blob\":\"{}\",\"lock_blob\":\"{}\"}}",
        blob(include_bytes!(
            "../../../../app/motion-tracker/src/communications/export.rs"
        )),
        blob(include_bytes!(
            "../../../../app/motion-tracker/src/clock.rs"
        )),
        blob(include_bytes!("capture.rs")),
        blob(include_bytes!("support/capture_protocol.rs")),
        blob(include_bytes!("../Cargo.toml")),
        blob(include_bytes!("../Cargo.lock")),
    );
    compare("nominal-two-second-stall", &nominal(200));
    compare("nominal-three-second-stall", &nominal(300));
    compare(
        "single-active-imu-stall",
        &(0..400)
            .map(|sequence| Record::new(1, sequence))
            .collect::<Vec<_>>(),
    );
    compare(
        "gnss-burst-stall",
        &(0..20)
            .map(|sequence| Record::new(3, sequence))
            .collect::<Vec<_>>(),
    );
    let uneven = (0..300)
        .map(|sequence| Record::new(1, sequence))
        .chain((0..100).map(|sequence| Record::new(2, sequence)))
        .chain((0..4).map(|sequence| Record::new(3, sequence)))
        .collect::<Vec<_>>();
    compare("uneven-source-burst", &uneven);
    // Start only after allocation windows; an assertion in one barrier
    // participant must fail the process instead of hanging a scoped join.
    let (done, finished) = mpsc::sync_channel(0);
    let watchdog = thread::spawn(move || {
        if finished.recv_timeout(Duration::from_secs(30)).is_err() {
            eprintln!("CAPTURE_FAILURE host witness deadline exceeded");
            std::process::abort();
        }
    });
    spsc_ownership_and_disconnect();
    spsc_concurrent_handoff();
    capture_concurrent_overflow_and_restart();
    epoch_check_is_not_a_commit_barrier();
    capture_protocol::run();
    done.send(()).unwrap();
    watchdog.join().unwrap();
    println!(
        "CAPTURE_GATES {{\"production_capture_changed\":false,\"candidate_integrated\":false,\"host_protocol_checked\":true,\"unmet\":[\"pooled-native-payload-transport\",\"protocol-integration-with-production-capture\",\"actual-owner-wake-clear-recheck-sleep\",\"partial-transport-and-client-compatibility\",\"declared-product-stall-and-memory-budgets\",\"native-atomic-and-scheduling-qualification\"],\"success\":true}}"
    );
}
