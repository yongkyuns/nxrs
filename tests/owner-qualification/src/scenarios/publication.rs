//! Exercise production navigation publication with paused peers and matched
//! resource measurements. Pauses are between public API calls, not inside an
//! atomic instruction or an unfinished publication. Timing is observational.

use super::{join, observe_wait, spawn, until};
use crate::{platform, Outcome, ALLOCATOR};
use allocation_probe::Snapshot as AllocationSnapshot;
use navigation::observations::{
    publication, Measurement, Observation, ObservationPublishers, ObservationReader, Observations,
    Reading, SensorFault, Snapshot,
};
use serde::Serialize;
use std::hint::black_box;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering::SeqCst};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread::{self, Thread};
use std::time::{Duration, Instant};

const READS: usize = 64;
const ADVANCES: usize = 64;
const ROUNDS: usize = 4096;
const IMU_FRESH: Duration = Duration::from_millis(500);
const GNSS_FRESH: Duration = Duration::from_secs(3);
const DEADLINE: Duration = Duration::from_secs(4);
type Publishers = ObservationPublishers<2, 2>;
type Reader = ObservationReader<2>;

#[derive(Serialize)]
pub struct Report {
    imu_sources: usize,
    readers: usize,
    paused_writer_reads: usize,
    paused_reader_publications: usize,
    pause_scope: &'static str,
    paused_writer: Option<platform::ThreadState>,
    paused_reader: Option<platform::ThreadState>,
    timestamps_preserved: bool,
    freshness_preserved: bool,
    sticky_faults_preserved: bool,
    late_samples_suppressed: bool,
    independent_sources: bool,
    native_configuration: Option<platform::MutexConfiguration>,
    comparison: Comparison,
}

#[derive(Serialize)]
struct Comparison {
    rounds: usize,
    imu_sources: usize,
    readers: usize,
    gnss_enabled: bool,
    scope: &'static str,
    memory_scope: &'static str,
    mutex: Cost,
    publication: Cost,
}

#[derive(Serialize)]
struct Cost {
    elapsed_ns: u64,
    setup: Window,
    first_use: Window,
    steady: Window,
    teardown: Window,
    fully_reclaimed: bool,
}

#[derive(Serialize)]
struct Window {
    alloc: usize,
    zeroed: usize,
    realloc: usize,
    dealloc: usize,
    failed: usize,
    live_before: usize,
    live_after: usize,
    blocks_before: usize,
    blocks_after: usize,
}

impl Window {
    fn new(before: AllocationSnapshot, after: AllocationSnapshot) -> Self {
        after.validate_after(before).unwrap();
        Self {
            alloc: after.alloc - before.alloc,
            zeroed: after.zeroed - before.zeroed,
            realloc: after.realloc - before.realloc,
            dealloc: after.dealloc - before.dealloc,
            failed: after.failed - before.failed,
            live_before: before.live,
            live_after: after.live,
            blocks_before: before.blocks,
            blocks_after: after.blocks,
        }
    }
}

#[derive(Default)]
struct Pause {
    ready: AtomicBool,
    release: AtomicBool,
    tid: AtomicI32,
    thread: OnceLock<Thread>,
}

impl Pause {
    fn wait(&self, tid: i32) {
        // Prewarm identity/parker before the marker. Subsequent waits use the
        // pinned std PRIO_NONE notification path, not a resource mutex.
        let current = thread::current();
        current.unpark();
        thread::park();
        self.thread.set(current).unwrap();
        self.tid.store(tid, SeqCst);
        let deadline = Instant::now() + DEADLINE;
        self.ready.store(true, SeqCst);
        while !self.release.load(SeqCst) {
            let remaining = deadline.saturating_duration_since(Instant::now());
            assert!(!remaining.is_zero(), "publication pause deadline");
            thread::park_timeout(remaining);
        }
    }

    fn resume(&self) {
        self.release.store(true, SeqCst);
        self.thread.get().unwrap().unpark();
    }

    fn state(&self) -> Option<platform::ThreadState> {
        assert!(!self.release.load(SeqCst));
        let observed = platform::thread_state(self.tid.load(SeqCst));
        if let Some(state) = observed {
            assert_eq!(state.base_priority, 120);
            assert_eq!(state.effective_priority, 120);
            assert_eq!(state.policy, platform::fifo_policy().unwrap());
            assert!(state.waiting_semaphore && !state.runnable);
        }
        observed
    }
}

fn sample(source: usize, sequence: u64) -> imu::ImuSample {
    imu::ImuSample {
        sequence,
        timestamp_us: sequence * 10_000 + source as u64,
        is_capture_time: true,
        sample_interval_us: 10_000,
        accel_mps2: [sequence as f32, source as f32, -3.0],
        gyro_rps: [4.0, -5.0, sequence as f32],
    }
}

fn fix(sequence: u64) -> gnss::GnssFix {
    gnss::GnssFix {
        sequence,
        timestamp_us: sequence * 1_000_000,
        is_capture_time: true,
        receiver_utc_ms: Some(1_500 + sequence as i64),
        latitude_deg: 12.0,
        longitude_deg: -14.0,
        speed_mps: 2.0,
        course_rad: Some(1.0),
        quality: None,
    }
}

fn satellites(sequence: u8, interval_ms: u32) -> gnss::SatelliteReport {
    gnss::SatelliteReport::new(
        26,
        24,
        Some(30),
        interval_ms,
        25,
        [gnss::SatelliteSignal {
            constellation: gnss::Constellation::Gps,
            id: sequence,
            cn0_db_hz: 31,
        }; 24],
    )
}

fn publish_all(publishers: &mut Publishers, sequence: u64, received: Instant) {
    for (index, publisher) in publishers.imu.iter_mut().enumerate() {
        assert!(publisher
            .record(&Observation::imu(
                index,
                imu::Event::Sample(sample(index, sequence)),
                received,
            ))
            .is_ok());
    }
    let receiver = publishers.gnss.as_mut().unwrap();
    assert!(receiver
        .record(&Observation::gnss(
            gnss::Event::Fix(fix(sequence)),
            received
        ))
        .is_ok());
    assert!(receiver
        .record(&Observation::gnss(
            gnss::Event::Satellites(satellites(sequence as u8, 3_000)),
            received,
        ))
        .is_ok());
}

fn assert_samples(snapshot: Snapshot<2>, sequences: [u64; 2]) {
    assert_eq!(
        snapshot.imu,
        [
            Reading::Live(sample(0, sequences[0])),
            Reading::Live(sample(1, sequences[1])),
        ]
    );
}

fn assert_complete(reader: &mut Reader, sequences: [u64; 2], gnss_sequence: u64, now: Instant) {
    let snapshot = reader.snapshot(now);
    assert_samples(snapshot, sequences);
    assert_eq!(snapshot.gnss, Some(Reading::Live(fix(gnss_sequence))));
    assert_eq!(
        reader.satellites(now),
        Reading::Live(satellites(gnss_sequence as u8, 3_000))
    );
}

fn progress(
    outcome: &mut Outcome,
) -> (Option<platform::ThreadState>, Option<platform::ThreadState>) {
    let now = Instant::now();
    let (mut publishers, [mut first, mut second]) =
        publication::<2, 2>(IMU_FRESH, Some(GNSS_FRESH));
    for reader in [&mut first, &mut second] {
        let initial = reader.snapshot(now);
        assert_eq!(initial.imu, [Reading::Waiting; 2]);
        assert_eq!(initial.gnss, Some(Reading::Waiting));
        assert_eq!(reader.satellites(now), Reading::Waiting);
    }
    publish_all(&mut publishers, 1, now);

    let writer_pause = Arc::new(Pause::default());
    let writer_done = Arc::new(AtomicBool::new(false));
    let writer = spawn("publication-writer-low", 120, {
        let pause = writer_pause.clone();
        let done = writer_done.clone();
        move |tid| {
            pause.wait(tid);
            publish_all(&mut publishers, 2, now);
            done.store(true, SeqCst);
            publishers
        }
    });
    observe_wait(
        &writer_pause.tid,
        120,
        || writer_pause.ready.load(SeqCst),
        outcome,
    );
    let reader_done = Arc::new(AtomicBool::new(false));
    let reader = spawn("publication-reader-high", 180, {
        let done = reader_done.clone();
        move |_| {
            for _ in 0..READS {
                assert_complete(&mut first, [1, 1], 1, now);
            }
            done.store(true, SeqCst);
            first
        }
    });
    until(|| reader_done.load(SeqCst));
    // Read back the low peer after high-priority progress, before releasing it.
    let paused_writer = writer_pause.state();
    writer_pause.resume();
    until(|| writer_done.load(SeqCst));
    let mut publishers = join(writer, outcome);
    let mut first = join(reader, outcome);
    assert_complete(&mut first, [2, 2], 2, now);

    let reader_pause = Arc::new(Pause::default());
    let reader_done = Arc::new(AtomicBool::new(false));
    let reader = spawn("publication-reader-low", 120, {
        let pause = reader_pause.clone();
        let done = reader_done.clone();
        move |tid| {
            let retained = second.snapshot(now);
            assert_samples(retained, [2, 2]);
            pause.wait(tid);
            assert_samples(retained, [2, 2]);
            assert_complete(&mut second, [2 + ADVANCES as u64, 2], 2, now);
            done.store(true, SeqCst);
            second
        }
    });
    observe_wait(
        &reader_pause.tid,
        120,
        || reader_pause.ready.load(SeqCst),
        outcome,
    );
    let writer_done = Arc::new(AtomicBool::new(false));
    let writer = spawn("publication-writer-high", 180, {
        let done = writer_done.clone();
        move |_| {
            for sequence in 3..=2 + ADVANCES as u64 {
                assert!(publishers.imu[0]
                    .record(&Observation::imu(
                        0,
                        imu::Event::Sample(sample(0, sequence)),
                        now,
                    ))
                    .is_ok());
                assert_complete(&mut first, [sequence, 2], 2, now);
            }
            done.store(true, SeqCst);
            (publishers, first)
        }
    });
    until(|| writer_done.load(SeqCst));
    let paused_reader = reader_pause.state();
    reader_pause.resume();
    until(|| reader_done.load(SeqCst));
    let (publishers, first) = join(writer, outcome);
    let second = join(reader, outcome);
    verify_contracts(publishers, [first, second], now);
    // All endpoints, phase gates and retained Thread handles drop before the
    // allocator comparison. No fixture worker remains alive at its snapshots.
    (paused_writer, paused_reader)
}

fn verify_contracts(mut publishers: Publishers, mut readers: [Reader; 2], received: Instant) {
    for reader in &mut readers {
        assert_complete(reader, [2 + ADVANCES as u64, 2], 2, received);
        assert_samples(
            reader.snapshot(received + IMU_FRESH - Duration::from_nanos(1)),
            [2 + ADVANCES as u64, 2],
        );
        let stale_imu = reader.snapshot(received + IMU_FRESH);
        assert_eq!(stale_imu.imu, [Reading::Stale; 2]);
        assert_eq!(stale_imu.gnss, Some(Reading::Live(fix(2))));
        assert_eq!(
            reader.snapshot(received + GNSS_FRESH).gnss,
            Some(Reading::Stale)
        );
        assert_eq!(
            reader.satellites(received + Duration::from_secs(6) - Duration::from_nanos(1)),
            Reading::Live(satellites(2, 3_000))
        );
        assert_eq!(
            reader.satellites(received + Duration::from_secs(6)),
            Reading::Stale
        );
    }

    let updated = received + IMU_FRESH;
    assert!(publishers.imu[1]
        .record(&Observation::imu(
            1,
            imu::Event::Sample(sample(1, 77)),
            updated
        ))
        .is_ok());
    assert!(publishers
        .gnss
        .as_mut()
        .unwrap()
        .record(&Observation::gnss(
            gnss::Event::Satellites(satellites(7, 10_000)),
            updated,
        ))
        .is_ok());
    for reader in &mut readers {
        let independent = reader.snapshot(updated);
        assert_eq!(
            independent.imu,
            [Reading::Stale, Reading::Live(sample(1, 77))]
        );
        assert_eq!(independent.gnss, Some(Reading::Live(fix(2))));
        assert_eq!(
            reader.snapshot(received + GNSS_FRESH).gnss,
            Some(Reading::Stale)
        );
        assert_eq!(
            reader.satellites(updated + Duration::from_secs(10)),
            Reading::Live(satellites(7, 10_000))
        );
        assert_eq!(
            reader.satellites(updated + Duration::from_secs(20)),
            Reading::Stale
        );
    }

    assert!(matches!(
        publishers.imu[0].record(&Observation::imu(
            0,
            imu::Event::Failed(imu::DeviceError::Io),
            updated,
        )),
        Err(SensorFault::Imu(0, imu::DeviceError::Io))
    ));
    assert!(matches!(
        publishers.imu[0].record(&Observation::imu(
            0,
            imu::Event::Failed(imu::DeviceError::Io),
            updated,
        )),
        Ok(None)
    ));
    // Late samples remain returned for an ordered capture path, while they
    // cannot revive the terminal latest state.
    match publishers.imu[0].record(&Observation::imu(
        0,
        imu::Event::Sample(sample(0, 88)),
        updated,
    )) {
        Ok(Some(Measurement::Imu(0, value))) => assert_eq!(value, sample(0, 88)),
        _ => panic!("late IMU capture value was not retained"),
    }
    let receiver = publishers.gnss.as_mut().unwrap();
    assert!(matches!(
        receiver.record(&Observation::gnss(
            gnss::Event::Failed(gnss::DeviceError::Io),
            updated
        )),
        Err(SensorFault::Gnss(gnss::DeviceError::Io))
    ));
    assert!(matches!(
        receiver.record(&Observation::gnss(
            gnss::Event::Failed(gnss::DeviceError::Io),
            updated
        )),
        Ok(None)
    ));
    match receiver.record(&Observation::gnss(gnss::Event::Fix(fix(9)), updated)) {
        Ok(Some(Measurement::Gnss(value))) => assert_eq!(value, fix(9)),
        _ => panic!("late GNSS capture value was not retained"),
    }
    assert!(matches!(
        receiver.record(&Observation::gnss(
            gnss::Event::Satellites(satellites(9, 3_000)),
            updated,
        )),
        Ok(None)
    ));
    for reader in &mut readers {
        let terminal = reader.snapshot(updated);
        assert_eq!(terminal.imu, [Reading::Fault, Reading::Live(sample(1, 77))]);
        assert_eq!(terminal.gnss, Some(Reading::Fault));
        assert_eq!(reader.satellites(updated), Reading::Fault);
        assert_eq!(
            reader.satellites(updated + Duration::from_secs(20)),
            Reading::Fault
        );
    }
}

struct MutexValues {
    writers: [Arc<Mutex<Observations<2>>>; 2],
    readers: [Arc<Mutex<Observations<2>>>; 2],
}

fn mutex_values() -> MutexValues {
    let values = Arc::new(Mutex::new(Observations::new(IMU_FRESH, None)));
    MutexValues {
        writers: [values.clone(), values.clone()],
        readers: [values.clone(), values],
    }
}

fn mutex_round(values: &mut MutexValues, sequence: u64, now: Instant) -> [Snapshot<2>; 2] {
    for (index, writer) in values.writers.iter().enumerate() {
        writer
            .lock()
            .unwrap()
            .imu_sample(index, sample(index, sequence), now);
    }
    values
        .readers
        .each_ref()
        .map(|reader| reader.lock().unwrap().snapshot(now))
}

fn publication_round(
    values: &mut (Publishers, [Reader; 2]),
    sequence: u64,
    now: Instant,
) -> [Snapshot<2>; 2] {
    for (index, publisher) in values.0.imu.iter_mut().enumerate() {
        let _ = publisher
            .record(&Observation::imu(
                index,
                imu::Event::Sample(sample(index, sequence)),
                now,
            ))
            .unwrap();
    }
    values.1.each_mut().map(|reader| reader.snapshot(now))
}

fn measure<T>(
    create: impl FnOnce() -> T,
    mut round: impl FnMut(&mut T, u64, Instant) -> [Snapshot<2>; 2],
    allocation_free_first_use: bool,
) -> Cost {
    let now = Instant::now();
    // All workers have joined. The probe may wait for an allocator hook and is
    // called only outside the measured operations, never on an acquisition path.
    let before = ALLOCATOR.snapshot();
    let mut values = create();
    let setup = ALLOCATOR.snapshot();
    let mut last = black_box(round(black_box(&mut values), black_box(1), now));
    let first_use = ALLOCATOR.snapshot();
    for snapshot in last {
        assert_samples(snapshot, [1, 1]);
        assert!(snapshot.gnss.is_none());
    }
    let started = Instant::now();
    for sequence in 2..=ROUNDS as u64 + 1 {
        last = black_box(round(black_box(&mut values), black_box(sequence), now));
    }
    let elapsed_ns = started.elapsed().as_nanos() as u64;
    let steady = ALLOCATOR.snapshot();
    for snapshot in last {
        assert_samples(snapshot, [ROUNDS as u64 + 1; 2]);
        assert!(snapshot.gnss.is_none());
    }
    drop(values);
    let teardown = ALLOCATOR.snapshot();

    assert!(elapsed_ns > 0);
    assert!(setup.live > before.live && setup.blocks > before.blocks);
    if allocation_free_first_use {
        first_use.no_allocator_calls_since(setup).unwrap();
    }
    steady.no_allocator_calls_since(first_use).unwrap();
    teardown.reclaimed_since(before).unwrap();
    Cost {
        elapsed_ns,
        setup: Window::new(before, setup),
        first_use: Window::new(setup, first_use),
        steady: Window::new(first_use, steady),
        teardown: Window::new(steady, teardown),
        fully_reclaimed: true,
    }
}

pub fn run() -> Outcome {
    let native_configuration = platform::mutex_configuration();
    if let Some(configuration) = native_configuration {
        assert!(configuration.priority_inheritance);
        assert!(configuration.default_prio_inherit);
        assert_eq!(
            configuration.default_protocol,
            configuration.protocol_inherit
        );
        assert_eq!(configuration.holder_capacity, 0);
        assert_eq!(platform::schedule().priority, Some(220));
    }
    let mut outcome = Outcome::default();
    let (paused_writer, paused_reader) = progress(&mut outcome);
    assert_eq!(outcome.joined, 4);
    assert_eq!(outcome.required_waits, 2);
    let comparison = Comparison {
        rounds: ROUNDS,
        imu_sources: 2,
        readers: 2,
        gnss_enabled: false,
        scope: "same-image-instrumented-uncontended",
        memory_scope: "requested-rust-global-allocator",
        mutex: measure(mutex_values, mutex_round, false),
        publication: measure(
            || publication::<2, 2>(IMU_FRESH, None),
            publication_round,
            true,
        ),
    };
    outcome.publication = Some(Report {
        imu_sources: 2,
        readers: 2,
        paused_writer_reads: READS,
        paused_reader_publications: ADVANCES,
        pause_scope: "between-production-api-calls",
        paused_writer,
        paused_reader,
        timestamps_preserved: true,
        freshness_preserved: true,
        sticky_faults_preserved: true,
        late_samples_suppressed: true,
        independent_sources: true,
        native_configuration,
        comparison,
    });
    outcome.endpoints_closed = true;
    outcome
}
