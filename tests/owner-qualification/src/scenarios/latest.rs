//! Compare fixed-reader copies without adopting a production backend.
use super::{join, spawn, until, Outcome};
use serde::Serialize;
use std::hint::black_box;
use std::mem::size_of;
use std::sync::atomic::{AtomicU8, Ordering::SeqCst};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use triple_buffer::triple_buffer;

const ROUNDS: usize = 4096;
const ADVANCES: u64 = 32;

#[derive(Clone, Copy, Debug, PartialEq)]
struct Stamped<T> {
    value: T,
    received: Instant,
}

#[derive(Serialize)]
pub struct Comparison {
    paused_writer_reads: usize,
    paused_reader_publications: u64,
    fanout_skew_checks: usize,
    rounds: usize,
    // Actual IMU, fix and satellite payloads, including their reception stamps.
    payloads: [Cost; 3],
}

#[derive(Serialize)]
struct Cost {
    record_bytes: usize,
    mutex_elapsed_ns: u64,
    mailbox_elapsed_ns: u64,
}

fn imu(sequence: u64) -> imu::ImuSample {
    imu::ImuSample {
        sequence,
        timestamp_us: sequence * 10_000,
        is_capture_time: true,
        sample_interval_us: 10_000,
        accel_mps2: [1.0, -2.0, 3.0],
        gyro_rps: [4.0, -5.0, 6.0],
    }
}

fn compare<T: Copy + Send>(value: T, received: Instant) -> Cost {
    let record = Some(Stamped { value, received });
    let mutex = Arc::new(Mutex::new(None));
    let readers = [mutex.clone(), mutex.clone()];
    let started = Instant::now();
    for _ in 0..ROUNDS {
        *mutex.lock().unwrap() = black_box(record);
        for reader in &readers {
            black_box(*reader.lock().unwrap());
        }
    }
    let mutex_elapsed_ns = started.elapsed().as_nanos() as u64;

    let (mut left, mut first) = triple_buffer(&None);
    let (mut right, mut second) = triple_buffer(&None);
    let started = Instant::now();
    for _ in 0..ROUNDS {
        let record = black_box(record);
        left.write(record);
        right.write(record);
        black_box(*first.read());
        black_box(*second.read());
    }
    Cost {
        record_bytes: size_of::<Option<Stamped<T>>>(),
        mutex_elapsed_ns,
        mailbox_elapsed_ns: started.elapsed().as_nanos() as u64,
    }
}

pub fn run() -> Outcome {
    let mut outcome = Outcome::default();
    let phase = Arc::new(AtomicU8::new(0));
    let resume = Arc::new(AtomicU8::new(0));
    let now = Instant::now();
    let initial = Stamped {
        value: imu(1),
        received: now,
    };
    let (mut left, mut first) = triple_buffer(&None);
    let (mut right, mut second) = triple_buffer(&None);
    assert_eq!((*first.read(), *second.read()), (None, None));
    left.write(Some(initial));
    right.write(Some(initial));
    let next = Stamped {
        value: imu(2),
        received: now,
    };
    let writer = spawn("paused-writer", 120, {
        let (phase, resume) = (phase.clone(), resume.clone());
        move |_| {
            let slot = left.input_buffer_mut();
            *slot = Some(initial);
            // Deliberately stop between pieces of a single record.
            slot.as_mut().unwrap().value.sequence = next.value.sequence;
            black_box(&mut *slot);
            phase.store(1, SeqCst);
            until(|| resume.load(SeqCst) == 1);
            *slot = Some(next);
            left.publish();
            phase.store(2, SeqCst);
            until(|| resume.load(SeqCst) == 2);
            right.write(Some(next));
            (left, right)
        }
    });
    until(|| phase.load(SeqCst) == 1);
    assert_eq!(
        (*first.read(), *second.read()),
        (Some(initial), Some(initial))
    );
    resume.store(1, SeqCst);
    until(|| phase.load(SeqCst) == 2);
    assert_eq!((*first.read(), *second.read()), (Some(next), Some(initial)));
    resume.store(2, SeqCst);
    let (mut left, mut right) = join(writer, &mut outcome);
    assert_eq!((*first.read(), *second.read()), (Some(next), Some(next)));

    phase.store(0, SeqCst);
    resume.store(0, SeqCst);
    let last = Stamped {
        value: imu(2 + ADVANCES),
        received: now,
    };
    let reader = spawn("paused-reader", 120, {
        let (phase, resume) = (phase.clone(), resume.clone());
        move |_| {
            let held = second.read();
            phase.store(1, SeqCst);
            until(|| resume.load(SeqCst) == 1);
            assert_eq!(*black_box(held), Some(next));
            assert_eq!(*second.read(), Some(last));
        }
    });
    until(|| phase.load(SeqCst) == 1);
    for sequence in 3..=2 + ADVANCES {
        let sample = Some(Stamped {
            value: imu(sequence),
            received: now,
        });
        left.write(sample);
        right.write(sample);
        assert_eq!(*first.read(), sample);
    }
    resume.store(1, SeqCst);
    join(reader, &mut outcome);
    drop((left, right, first));

    let fix = gnss::GnssFix {
        sequence: 5,
        timestamp_us: 700,
        is_capture_time: true,
        receiver_utc_ms: Some(1500),
        latitude_deg: 12.0,
        longitude_deg: -14.0,
        speed_mps: 2.0,
        course_rad: Some(1.0),
        quality: None,
    };
    let signals = [gnss::SatelliteSignal {
        constellation: gnss::Constellation::Gps,
        id: 5,
        cn0_db_hz: 31,
    }; 24];
    let satellites = gnss::SatelliteReport::new(26, 24, Some(30), 3_000, 25, signals);
    outcome.latest = Some(Comparison {
        paused_writer_reads: 2,
        paused_reader_publications: ADVANCES,
        fanout_skew_checks: 1,
        rounds: ROUNDS,
        payloads: [
            compare(imu(1), now),
            compare(fix, now),
            compare(satellites, now),
        ],
    });
    outcome.endpoints_closed = true;
    outcome
}
