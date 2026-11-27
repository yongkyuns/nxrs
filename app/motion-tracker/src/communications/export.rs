//! Bounded capture for the authenticated tracker export stream.
use crate::clock::ClockAnchor;
use navigation::observations::Measurement;
use std::collections::VecDeque;
use std::io;
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Mutex, OnceLock,
};
use std::thread::{self, Thread};
use std::time::Duration;

pub const REQUEST_PREFIX: &[u8; 4] = b"NXRS";
pub const REQUEST_LEN: usize = REQUEST_PREFIX.len() + 1;
// Roughly 2.5 seconds at two 100 Hz IMUs and 2 Hz GNSS. Retain samples
// while connection-level backpressure clears and the exporter catches up.
const QUEUE: usize = 512;
const BATCH: usize = 16;
const RECORD_HEADER: usize = 13;
const IMU_BYTES: usize = RECORD_HEADER + 24;
const GNSS_BYTES: usize = RECORD_HEADER + 20;
const UTC_UNAVAILABLE: i64 = i64::MIN;
const DELTA: u8 = 0x80;
const COUNTS: u8 = 0x40;
const NO_UTC: u8 = 0x20;
pub const MAX_BYTES: usize = 4 + 8 + BATCH * IMU_BYTES;

#[derive(Clone, Copy)]
pub enum Version {
    Fixed = 1,
    Compact = 2,
    Timed = 3,
}

impl Version {
    pub fn from_request(value: u8) -> Option<Self> {
        match value {
            1 => Some(Self::Fixed),
            2 => Some(Self::Compact),
            3 => Some(Self::Timed),
            _ => None,
        }
    }

    pub fn batch_interval(self) -> Duration {
        Duration::from_millis(if matches!(self, Self::Timed) { 75 } else { 40 })
    }
}

pub struct Batch {
    pub len: usize,
    pub terminal: bool,
}

#[derive(Default)]
pub struct Capture {
    // Odd epochs admit records. A new export has a different epoch so an
    // acquisition task paused during encoding cannot enqueue into it.
    epoch: AtomicUsize,
    ready: AtomicBool,
    sensor_failed: AtomicBool,
    owner: OnceLock<Thread>,
    samples: Mutex<Samples>,
    scales: [Option<[u32; 2]>; 2],
    clock: OnceLock<ClockAnchor>,
}

#[derive(Default)]
struct Samples {
    queue: VecDeque<Sample>,
    first_missing: Option<(Source, u32)>,
    high_water: usize,
}

#[derive(Clone, Copy)]
enum Source {
    Imu(u8),
    Gnss,
}

impl Source {
    fn tag(self) -> u8 {
        match self {
            Self::Imu(index) => 1 + index,
            Self::Gnss => 3,
        }
    }
}

/// The export queue retains only the scalar fields its consumer needs.
/// Float bits have native byte order here and are serialized by the owner.
#[derive(Clone, Copy)]
struct Sample {
    time: u64,
    sequence: u32,
    source: Source,
    values: [u32; 6],
}

impl From<Measurement> for Sample {
    fn from(sample: Measurement) -> Self {
        match sample {
            Measurement::Imu(index, sample) => {
                let [ax, ay, az] = sample.accel_mps2.map(f32::to_bits);
                let [gx, gy, gz] = sample.gyro_rps.map(f32::to_bits);
                Self {
                    time: sample.timestamp_us / 1_000,
                    sequence: sample.sequence as u32,
                    source: Source::Imu(index as u8),
                    values: [ax, ay, az, gx, gy, gz],
                }
            }
            Measurement::Gnss(fix) => {
                let lat = fix.latitude_deg.to_bits();
                let lon = fix.longitude_deg.to_bits();
                Self {
                    time: fix.timestamp_us / 1_000,
                    sequence: fix.sequence as u32,
                    source: Source::Gnss,
                    values: [
                        lat as u32,
                        (lat >> 32) as u32,
                        lon as u32,
                        (lon >> 32) as u32,
                        fix.speed_mps.to_bits(),
                        0,
                    ],
                }
            }
        }
    }
}

impl Capture {
    pub fn new(scales: [Option<[u32; 2]>; 2]) -> Self {
        Self {
            scales,
            ..Self::default()
        }
    }

    pub fn scales(&self) -> [Option<[u32; 2]>; 2] {
        self.scales
    }

    pub fn clock(&self) -> Option<ClockAnchor> {
        self.clock.get().copied()
    }

    pub fn register_owner(&self) {
        // The communications task lives across individual export sessions.
        assert!(self.owner.set(thread::current()).is_ok());
    }

    pub fn take_ready(&self) -> bool {
        self.ready.swap(false, Ordering::AcqRel)
    }

    pub fn sensor_failed(&self) {
        self.sensor_failed.store(true, Ordering::Release);
        if let Some(owner) = self.owner.get() {
            owner.unpark();
        }
    }

    pub fn take_sensor_failed(&self) -> bool {
        self.sensor_failed.swap(false, Ordering::AcqRel)
    }

    pub fn enable(&self) -> io::Result<()> {
        // An export reconnect may overlap an acquisition callback. Reserve
        // before touching the queue lock, and retire the old queue outside it.
        let mut queue = VecDeque::new();
        queue
            .try_reserve_exact(QUEUE)
            .map_err(|_| io::ErrorKind::OutOfMemory)?;
        {
            let mut samples = self.samples.lock().unwrap();
            std::mem::swap(&mut samples.queue, &mut queue);
            samples.first_missing = None;
            samples.high_water = 0;
            self.ready.store(false, Ordering::Relaxed);
            let epoch = self.epoch.load(Ordering::Relaxed).wrapping_add(1) | 1;
            self.epoch.store(epoch, Ordering::Release);
        }
        Ok(())
    }

    pub fn finish(&self) -> usize {
        // Move the queue out so its storage is freed after unlocking acquisition.
        let (_retired_queue, high_water) = {
            let mut samples = self.samples.lock().unwrap();
            let epoch = self.epoch.load(Ordering::Relaxed);
            self.epoch
                .store(epoch.wrapping_add(epoch & 1), Ordering::Release);
            self.ready.store(false, Ordering::Relaxed);
            samples.first_missing = None;
            (std::mem::take(&mut samples.queue), samples.high_water)
        };
        high_water
    }

    pub fn record(&self, sample: Measurement) {
        if let Measurement::Gnss(fix) = sample {
            if let Some(anchor) = fix
                .receiver_utc_ms
                .and_then(|utc| ClockAnchor::new(fix.timestamp_us / 1_000, utc))
            {
                let _ = self.clock.set(anchor);
            }
        }
        let epoch = self.epoch.load(Ordering::Acquire);
        if epoch & 1 == 0 {
            return;
        }
        self.enqueue(epoch, sample.into());
    }

    fn enqueue(&self, epoch: usize, sample: Sample) {
        // This runs in the acquisition task. The consumer holds the lock
        // only for a bounded copy, with no encoding or transport I/O.
        {
            let mut samples = self.samples.lock().unwrap();
            if self.epoch.load(Ordering::Relaxed) != epoch {
                return;
            }
            if samples.queue.len() == QUEUE {
                samples.first_missing = Some((sample.source, sample.sequence));
                self.epoch.store(epoch.wrapping_add(1), Ordering::Release);
            } else {
                samples.queue.push_back(sample);
                let depth = samples.queue.len();
                samples.high_water = samples.high_water.max(depth);
                if depth != BATCH {
                    return;
                }
            }
            self.ready.store(true, Ordering::Release);
        }
        if let Some(owner) = self.owner.get() {
            owner.unpark();
        }
    }

    pub fn take(&self, output: &mut [u8; MAX_BYTES], due: bool, version: Version) -> Option<Batch> {
        let mut records = [None; BATCH];
        let count = {
            let mut samples = self.samples.lock().unwrap();
            let count = samples.queue.len().min(BATCH);
            // Full batches can catch up while the deadline bounds how
            // long a smaller batch waits to fill.
            if count < BATCH && !due && samples.first_missing.is_none() {
                return None;
            }
            if count == 0 {
                let (source, sequence) = samples.first_missing?;
                let len = match version {
                    Version::Fixed => 4,
                    Version::Compact | Version::Timed => {
                        output[4] = source.tag();
                        output[5..9].copy_from_slice(&sequence.to_le_bytes());
                        9
                    }
                };
                header(output, version, len);
                return Some(Batch {
                    len,
                    terminal: true,
                });
            }
            for (record, sample) in records.iter_mut().zip(samples.queue.drain(..count)) {
                *record = Some(sample);
            }
            count
        };

        let timed = matches!(version, Version::Timed);
        let clock = self.clock();
        let mut previous = [None::<(u32, u64)>; 3];
        let mut end = 4;
        if timed {
            output[end..end + 8].copy_from_slice(
                &clock
                    .map_or(UTC_UNAVAILABLE, |anchor| anchor.offset_ms)
                    .to_le_bytes(),
            );
            end += 8;
        }
        for sample in records[..count].iter().flatten() {
            let kind = sample.source.tag();
            let index = usize::from(kind - 1);
            let len = if kind == 3 { GNSS_BYTES } else { IMU_BYTES };
            let sequence = sample.sequence;
            let time = sample.time;
            let last = &mut previous[index];
            let delta = last
                .filter(|(seq, _)| {
                    seq.wrapping_add(1) == sequence && !matches!(version, Version::Fixed)
                })
                .and_then(|(_, prior)| time.checked_sub(prior))
                .and_then(|elapsed| u8::try_from(elapsed).ok());
            *last = Some((sequence, time));

            let mut payload = [0; IMU_BYTES - RECORD_HEADER];
            for (bytes, value) in payload.as_chunks_mut::<4>().0.iter_mut().zip(sample.values) {
                bytes.copy_from_slice(&value.to_le_bytes());
            }
            let payload = &payload[..len - RECORD_HEADER];
            let mut tag = kind;
            if timed && clock.is_some_and(|anchor| time < anchor.since_ms) {
                tag |= NO_UTC;
            }
            let counts = if timed && kind != 3 {
                self.scales[index].and_then(|scale| encode_counts(payload, scale))
            } else {
                None
            };
            if counts.is_some() {
                tag |= COUNTS;
            }
            if let Some(delta) = delta {
                output[end] = tag | DELTA;
                output[end + 1] = delta;
                end += 2;
            } else {
                output[end] = tag;
                output[end + 1..end + 5].copy_from_slice(&sequence.to_le_bytes());
                let time_bytes = if timed { 8 } else { 4 };
                output[end + 5..end + 5 + time_bytes]
                    .copy_from_slice(&time.to_le_bytes()[..time_bytes]);
                end += 5 + time_bytes;
            }
            let payload = counts.as_ref().map_or(payload, |bytes| bytes.as_slice());
            output[end..end + payload.len()].copy_from_slice(payload);
            end += payload.len();
        }
        header(output, version, end);
        Some(Batch {
            len: end,
            terminal: false,
        })
    }
}

/// Encode only if the decoder's multiplication reproduces every f32 bit.
/// Other HALs and measurements outside the declared grid keep their floats.
fn encode_counts(payload: &[u8], scale: [u32; 2]) -> Option<[u8; 12]> {
    let mut counts = [0; 12];
    let scale = scale.map(f32::from_bits);
    for (axis, bytes) in payload.as_chunks::<4>().0.iter().enumerate() {
        let bits = u32::from_le_bytes(*bytes);
        let scale = scale[axis / 3];
        let count = (f32::from_bits(bits) / scale).round() as i16;
        if (f32::from(count) * scale).to_bits() != bits {
            return None;
        }
        counts[axis * 2..axis * 2 + 2].copy_from_slice(&count.to_le_bytes());
    }
    Some(counts)
}

fn header(output: &mut [u8; MAX_BYTES], version: Version, len: usize) {
    output[..2].copy_from_slice(&[0xa5, version as u8]);
    output[2..4].copy_from_slice(&((len - 4) as u16).to_le_bytes());
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{sync::mpsc, thread, time::Duration};

    fn fix(sequence: u64) -> Measurement {
        Measurement::Gnss(gnss::GnssFix {
            sequence,
            timestamp_us: sequence * 500_000,
            is_capture_time: false,
            receiver_utc_ms: None,
            latitude_deg: 45.0,
            longitude_deg: -76.0,
            speed_mps: 2.0,
            course_rad: None,
            quality: None,
        })
    }

    #[test]
    fn arrival_during_queue_access_is_delivered() {
        let capture = Capture::default();
        capture.enable().unwrap();
        thread::scope(|scope| {
            let (started, ready) = mpsc::channel();
            let (finished, complete) = mpsc::channel();
            let capture = &capture;
            let early = {
                let _queue = capture.samples.lock().unwrap();
                scope.spawn(move || {
                    started.send(()).unwrap();
                    capture.record(fix(1));
                    finished.send(()).unwrap();
                });
                ready.recv().unwrap();
                complete.recv_timeout(Duration::from_millis(40))
            };
            assert_eq!(early, Err(mpsc::RecvTimeoutError::Timeout));
            complete.recv().unwrap();
        });
        let mut bytes = [0; MAX_BYTES];
        assert!(capture.take(&mut bytes, false, Version::Fixed).is_none());
        assert_eq!(
            capture.take(&mut bytes, true, Version::Fixed).unwrap().len,
            33
        );
        assert_eq!(bytes[4], 3);
        assert_eq!(&bytes[5..9], &1_u32.to_le_bytes());
    }

    #[test]
    fn sensor_fault_is_retained_while_export_owns_the_queue() {
        let capture = Capture::default();
        let (finished, complete) = mpsc::channel();
        thread::scope(|scope| {
            let notified = {
                let _queue = capture.samples.lock().unwrap();
                scope.spawn(|| {
                    capture.sensor_failed();
                    finished.send(()).unwrap();
                });
                complete.recv_timeout(Duration::from_secs(1))
            };
            assert_eq!(notified, Ok(()));
        });
        assert!(capture.take_sensor_failed());
        assert!(!capture.take_sensor_failed());
    }

    #[test]
    fn overflow_drains_admitted_records_and_stops_the_export() {
        let capture = Capture::default();
        capture.enable().unwrap();
        for sequence in 0..=(QUEUE + 1) {
            capture.record(fix(sequence as u64));
        }
        let mut bytes = [0; MAX_BYTES];
        for first in (0..QUEUE).step_by(BATCH) {
            let batch = capture.take(&mut bytes, false, Version::Fixed).unwrap();
            assert!(!batch.terminal);
            assert_eq!(batch.len, 4 + BATCH * 29);
            assert_eq!(&bytes[5..9], &(first as u32).to_le_bytes());
        }
        let failure = capture.take(&mut bytes, false, Version::Compact).unwrap();
        assert!(failure.terminal);
        assert_eq!(&bytes[..failure.len], &[0xa5, 2, 5, 0, 3, 0, 2, 0, 0]);
        let failure = capture.take(&mut bytes, false, Version::Fixed).unwrap();
        assert!(failure.terminal);
        assert_eq!(&bytes[..failure.len], &[0xa5, 1, 0, 0]);
        capture.enable().unwrap();
        assert!(capture.take(&mut bytes, true, Version::Fixed).is_none());
        capture.record(fix(QUEUE as u64 + 2));
        assert_eq!(
            capture.take(&mut bytes, true, Version::Fixed).unwrap().len,
            33
        );
    }

    #[test]
    fn disconnect_releases_queue_and_rejects_work_from_the_old_export() {
        let capture = Capture::default();
        capture.enable().unwrap();
        let old_epoch = capture.epoch.load(Ordering::Relaxed);
        capture.record(fix(1));
        assert_eq!(capture.finish(), 1);
        assert_eq!(capture.samples.lock().unwrap().queue.capacity(), 0);

        capture.enable().unwrap();
        capture.enqueue(old_epoch, fix(2).into());
        capture.record(fix(3));
        let mut bytes = [0; MAX_BYTES];
        let batch = capture.take(&mut bytes, true, Version::Fixed).unwrap();
        assert_eq!(batch.len, 33);
        assert_eq!(&bytes[5..9], &3_u32.to_le_bytes());
    }

    #[test]
    fn timed_batch_preserves_native_counts_and_excludes_pre_anchor_samples() {
        let scale = [0.125_f32.to_bits(), 0.25_f32.to_bits()];
        let capture = Capture::new([Some(scale), None]);
        capture.enable().unwrap();
        let first = u64::from(u32::MAX);
        let imu = |sequence, time| {
            Measurement::Imu(
                0,
                imu::ImuSample {
                    sequence,
                    timestamp_us: time * 1_000 + 700,
                    is_capture_time: true,
                    sample_interval_us: 10_000,
                    accel_mps2: [-4096.0, 0.0, 4095.875],
                    gyro_rps: [-0.25, 1.0, 8191.75],
                },
            )
        };
        capture.record(imu(1, first));
        let Measurement::Gnss(mut fix) = fix(1) else {
            unreachable!()
        };
        fix.timestamp_us = (first + 3) * 1_000;
        fix.receiver_utc_ms = Some(1_767_225_600_000);
        capture.record(Measurement::Gnss(fix));
        capture.record(imu(2, first + 9));

        let mut bytes = [0; MAX_BYTES];
        let frame = capture.take(&mut bytes, true, Version::Timed).unwrap();
        assert_eq!(frame.len, 4 + 8 + 25 + 33 + 14);
        let expected_offset = 1_767_225_600_000 - (first + 3) as i64;
        assert_eq!(bytes[4..12], expected_offset.to_le_bytes());
        assert_eq!(bytes[12], 1 | COUNTS | NO_UTC);
        assert_eq!(bytes[17..25], first.to_le_bytes());
        assert_eq!(
            bytes[25..37],
            [0, 128, 0, 0, 255, 127, 255, 255, 4, 0, 255, 127]
        );
        assert_eq!(bytes[37], 3);
        assert_eq!(bytes[42..50], (first + 3).to_le_bytes());
        assert_eq!(bytes[70..72], [1 | COUNTS | DELTA, 9]);
        assert_eq!(capture.clock().unwrap().utc_ms(first), None);
        assert_eq!(
            capture.clock().unwrap().utc_ms(first + 9),
            Some(1_767_225_600_006)
        );
    }

    #[test]
    fn counts_are_bit_exact_or_use_the_float_record() {
        let scale = [f32::to_bits(0.125), f32::to_bits(0.25)];
        let mut payload = [0; 24];
        for (axis, slot) in payload.as_chunks_mut::<4>().0.iter_mut().enumerate() {
            let value = f32::from(i16::MIN) * f32::from_bits(scale[axis / 3]);
            slot.copy_from_slice(&value.to_le_bytes());
        }
        assert_eq!(
            encode_counts(&payload, scale),
            Some([0, 128].repeat(6).try_into().unwrap())
        );
        payload[..4].copy_from_slice(&0.1_f32.to_le_bytes());
        assert_eq!(encode_counts(&payload, scale), None);
    }

    #[test]
    fn compact_records_preserve_floats_and_use_full_headers_across_gaps() {
        let capture = Capture::default();
        capture.enable().unwrap();
        let point = |sequence, timestamp_ms: u64| {
            Measurement::Imu(
                0,
                imu::ImuSample {
                    sequence,
                    timestamp_us: timestamp_ms * 1_000,
                    is_capture_time: true,
                    sample_interval_us: 10_000,
                    accel_mps2: [1.25, -0.5, 9.75],
                    gyro_rps: [0., 1., -2.],
                },
            )
        };
        for sample in [point(15, 100), point(16, 111), point(18, 130)] {
            capture.record(sample);
        }
        let mut bytes = [0; MAX_BYTES];
        let frame = capture.take(&mut bytes, true, Version::Compact).unwrap();
        assert_eq!(frame.len, 4 + 33 + 26 + 33);
        assert_eq!(&bytes[37..39], &[0x81, 11]);
        assert_eq!(&bytes[39..63], &bytes[13..37]);
        assert_eq!(&bytes[63..72], &[1, 18, 0, 0, 0, 130, 0, 0, 0]);
    }
}
