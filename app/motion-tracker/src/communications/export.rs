//! Bounded capture for the authenticated tracker export stream.
use crate::clock::ClockAnchor;
use navigation::observations::{Measurement, SensorFault};
use std::collections::VecDeque;
use std::io;
use std::sync::{
    atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering},
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
const FAILED: usize = 1 << (usize::BITS - 1);
pub const MAX_BYTES: usize = 4 + 8 + BATCH * IMU_BYTES;

#[derive(Clone, Copy)]
pub enum Version {
    Fixed = 1,
    Compact = 2,
    Timed = 3,
    /// Capture-time provenance, source unavailability and typed terminal faults.
    Evidence = 4,
}

impl Version {
    pub fn from_request(value: u8) -> Option<Self> {
        match value {
            1 => Some(Self::Fixed),
            2 => Some(Self::Compact),
            3 => Some(Self::Timed),
            4 => Some(Self::Evidence),
            _ => None,
        }
    }

    pub fn batch_interval(self) -> Duration {
        Duration::from_millis(if matches!(self, Self::Timed | Self::Evidence) {
            75
        } else {
            40
        })
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
    // Source/cause survives a full queue and a missed owner notification.
    first_fault: AtomicU32,
    owner: OnceLock<Mutex<Thread>>,
    samples: Mutex<Samples>,
    scales: [Option<[u32; 2]>; 2],
    clock: Mutex<Option<ClockAnchor>>,
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
    GnssUnavailable,
}

impl Source {
    fn tag(self) -> u8 {
        match self {
            Self::Imu(index) => 1 + index,
            Self::Gnss => 3,
            Self::GnssUnavailable => 4,
        }
    }
}

/// The export queue retains only the scalar fields its consumer needs.
/// Float bits have native byte order here and are serialized by the owner.
#[derive(Clone, Copy)]
struct Sample {
    time: u64,
    // Capture the applicable mapping at admission. A later fix or NoFix must
    // never re-date the admitted prefix while transport is backpressured.
    utc_offset_ms: i64,
    sequence: u32,
    capture_time: bool,
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
                    utc_offset_ms: UTC_UNAVAILABLE,
                    sequence: sample.sequence as u32,
                    capture_time: sample.is_capture_time,
                    source: Source::Imu(index as u8),
                    values: [ax, ay, az, gx, gy, gz],
                }
            }
            Measurement::Gnss(fix) => {
                let lat = fix.latitude_deg.to_bits();
                let lon = fix.longitude_deg.to_bits();
                Self {
                    time: fix.timestamp_us / 1_000,
                    utc_offset_ms: UTC_UNAVAILABLE,
                    sequence: fix.sequence as u32,
                    capture_time: fix.is_capture_time,
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
        *self.clock.lock().unwrap()
    }

    pub fn register_owner(&self) {
        let current = thread::current();
        let owner = self.owner.get_or_init(|| {
            let owner = Mutex::new(current.clone());
            // Some targets initialize the underlying mutex on first use.
            // Do that on the communications worker before publishing it.
            drop(owner.lock().unwrap());
            owner
        });
        let retired = std::mem::replace(&mut *owner.lock().unwrap(), current);
        drop(retired);
    }

    fn notify_owner(&self) {
        // Readiness and faults are retained if registration or another
        // notification holds the lock. Active export also has a poll deadline.
        if let Some(owner) = self.owner.get() {
            if let Ok(owner) = owner.try_lock() {
                owner.unpark();
            }
        }
    }

    pub fn take_ready(&self) -> bool {
        self.ready.swap(false, Ordering::AcqRel)
    }

    pub fn source_failed(&self, fault: SensorFault) {
        let (source, reason) = match fault {
            SensorFault::Imu(index, error) if index < 2 => {
                let reason = match error {
                    imu::DeviceError::Unsupported => 1,
                    imu::DeviceError::Busy => 2,
                    imu::DeviceError::Timeout => 3,
                    imu::DeviceError::Full => 4,
                    imu::DeviceError::Io => 5,
                    imu::DeviceError::InvalidData => 6,
                };
                (index as u32 + 1, reason)
            }
            SensorFault::Imu(_, _) => return,
            SensorFault::Gnss(error) => {
                let reason = match error {
                    gnss::DeviceError::Unsupported => 1,
                    gnss::DeviceError::Busy => 2,
                    gnss::DeviceError::Timeout => 3,
                    gnss::DeviceError::Full => 4,
                    gnss::DeviceError::Io => 5,
                    gnss::DeviceError::InvalidData => 6,
                };
                *self.clock.lock().unwrap() = None;
                (3, reason)
            }
        };
        let _ = self.first_fault.compare_exchange(
            0,
            source | reason << 8,
            Ordering::AcqRel,
            Ordering::Acquire,
        );
        // Close admission before waking the owner. A paused producer is rejected
        // by its epoch check; the admitted prefix drains before the fault record.
        self.epoch.fetch_or(FAILED, Ordering::AcqRel);
        self.ready.store(true, Ordering::Release);
        self.sensor_failed();
    }

    pub fn gnss_unavailable(&self, status: gnss::NoFix) {
        *self.clock.lock().unwrap() = None;
        let epoch = self.epoch.load(Ordering::Acquire);
        if epoch & (FAILED | 1) == 1 {
            let source_epoch = status.source_epoch_ms.unwrap_or(u64::MAX);
            self.enqueue(
                epoch,
                Sample {
                    time: status.received_us / 1_000,
                    utc_offset_ms: UTC_UNAVAILABLE,
                    sequence: status.generation,
                    capture_time: false,
                    source: Source::GnssUnavailable,
                    values: [source_epoch as u32, (source_epoch >> 32) as u32, 0, 0, 0, 0],
                },
            );
        }
        self.sensor_failed(); // retained state changed; wake the telemetry owner
    }

    pub fn sensor_failed(&self) {
        self.sensor_failed.store(true, Ordering::Release);
        self.notify_owner();
    }

    pub fn take_sensor_failed(&self) -> bool {
        self.sensor_failed.swap(false, Ordering::AcqRel)
    }

    /// A v4 peer can still receive the retained typed failure after admission
    /// is refused. This predicate never authorizes new sample admission.
    pub fn is_failed(&self) -> bool {
        self.first_fault.load(Ordering::Acquire) != 0
    }

    pub fn enable(&self) -> io::Result<()> {
        if self.first_fault.load(Ordering::Acquire) != 0 {
            return Err(io::Error::new(
                io::ErrorKind::NotConnected,
                "capture source unavailable",
            ));
        }
        // An export reconnect may overlap an acquisition callback. Reserve
        // before touching the queue lock, and retire the old queue outside it.
        let mut queue = VecDeque::new();
        queue
            .try_reserve_exact(QUEUE)
            .map_err(|_| io::ErrorKind::OutOfMemory)?;
        {
            let mut samples = self.samples.lock().unwrap();
            if self.first_fault.load(Ordering::Acquire) != 0 {
                return Err(io::Error::new(
                    io::ErrorKind::NotConnected,
                    "capture source unavailable",
                ));
            }
            let prior = self.epoch.load(Ordering::Acquire);
            let epoch = prior
                .checked_add(if prior & 1 == 0 { 1 } else { 2 })
                .filter(|value| *value < FAILED - 1)
                .ok_or_else(|| io::Error::other("capture generation exhausted or source failed"))?;
            self.epoch
                .compare_exchange(prior, epoch, Ordering::AcqRel, Ordering::Acquire)
                .map_err(|_| io::Error::other("capture source failed during admission"))?;
            std::mem::swap(&mut samples.queue, &mut queue);
            samples.first_missing = None;
            samples.high_water = 0;
            self.ready.store(false, Ordering::Relaxed);
        }
        Ok(())
    }

    pub fn finish(&self) -> usize {
        // Move the queue out so its storage is freed after unlocking acquisition.
        let (_retired_queue, high_water) = {
            let mut samples = self.samples.lock().unwrap();
            let epoch = self.epoch.load(Ordering::Relaxed);
            self.epoch.fetch_add(epoch & 1, Ordering::AcqRel);
            self.ready.store(false, Ordering::Relaxed);
            samples.first_missing = None;
            (std::mem::take(&mut samples.queue), samples.high_water)
        };
        high_water
    }

    pub fn record(&self, sample: Measurement) {
        let epoch = self.epoch.load(Ordering::Acquire);
        let clock = {
            let mut clock = self.clock.lock().unwrap();
            if let Measurement::Gnss(fix) = sample {
                *clock = ClockAnchor::from_fix(fix);
            }
            *clock
        };
        if epoch & (FAILED | 1) != 1 {
            return;
        }
        let mut sample = Sample::from(sample);
        if sample.capture_time {
            sample.utc_offset_ms = clock
                .filter(|anchor| anchor.utc_ms(sample.time).is_some())
                .map_or(UTC_UNAVAILABLE, |anchor| anchor.offset_ms);
        }
        self.enqueue(epoch, sample);
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
                self.epoch.fetch_add(1, Ordering::AcqRel);
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
        self.notify_owner();
    }

    pub fn take(&self, output: &mut [u8; MAX_BYTES], due: bool, version: Version) -> Option<Batch> {
        let mut records = [None; BATCH];
        let timed = matches!(version, Version::Timed | Version::Evidence);
        let mut utc_offset_ms = UTC_UNAVAILABLE;
        let count = {
            let mut samples = self.samples.lock().unwrap();
            let count = samples.queue.len().min(BATCH);
            // Full batches can catch up while the deadline bounds how
            // long a smaller batch waits to fill.
            if count < BATCH
                && !due
                && samples.first_missing.is_none()
                && self.first_fault.load(Ordering::Acquire) == 0
            {
                return None;
            }
            if count == 0 {
                let Some((source, sequence)) = samples.first_missing else {
                    let fault = self.first_fault.load(Ordering::Acquire);
                    if fault == 0 {
                        return None;
                    }
                    if matches!(version, Version::Evidence) {
                        output[4] = fault as u8;
                        output[5] = (fault >> 8) as u8;
                        header(output, version, 6);
                        return Some(Batch {
                            len: 6,
                            terminal: true,
                        });
                    }
                    // Legacy peers cannot name this terminal condition. End the
                    // connection after its admitted prefix, never claim overflow
                    // or normal completion for a failed source.
                    return Some(Batch {
                        len: 0,
                        terminal: true,
                    });
                };
                if matches!(source, Source::GnssUnavailable) {
                    if matches!(version, Version::Evidence) {
                        output[4] = 3;
                        output[5] = 7; // GNSS availability evidence was lost
                        header(output, version, 6);
                        return Some(Batch {
                            len: 6,
                            terminal: true,
                        });
                    }
                    return Some(Batch {
                        len: 0,
                        terminal: true,
                    });
                }
                let len = match version {
                    Version::Fixed => 4,
                    Version::Compact | Version::Timed | Version::Evidence => {
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
            let mut accepted = 0;
            for _ in 0..count {
                let sample = *samples.queue.front().unwrap();
                // v3/v4 have one offset per frame. End a batch before a new
                // mapping segment, without changing any queued sample's UTC.
                if timed
                    && sample.utc_offset_ms != UTC_UNAVAILABLE
                    && utc_offset_ms != UTC_UNAVAILABLE
                    && sample.utc_offset_ms != utc_offset_ms
                {
                    break;
                }
                samples.queue.pop_front();
                if matches!(sample.source, Source::GnssUnavailable)
                    && !matches!(version, Version::Evidence)
                {
                    continue;
                }
                if sample.utc_offset_ms != UTC_UNAVAILABLE {
                    utc_offset_ms = sample.utc_offset_ms;
                }
                records[accepted] = Some(sample);
                accepted += 1;
            }
            if accepted == 0 {
                return None;
            }
            accepted
        };

        let mut previous = [None::<(u32, u64)>; 3];
        let mut end = 4;
        if timed {
            output[end..end + 8].copy_from_slice(&utc_offset_ms.to_le_bytes());
            end += 8;
        }
        for sample in records[..count].iter().flatten() {
            let kind = sample.source.tag();
            if matches!(sample.source, Source::GnssUnavailable) {
                output[end] = 4;
                output[end + 1..end + 5].copy_from_slice(&sample.sequence.to_le_bytes());
                output[end + 5..end + 13].copy_from_slice(&sample.time.to_le_bytes());
                for (bytes, value) in output[end + 13..end + 21]
                    .chunks_exact_mut(4)
                    .zip(sample.values)
                {
                    bytes.copy_from_slice(&value.to_le_bytes());
                }
                end += 21;
                continue;
            }
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
            for (bytes, value) in payload.chunks_exact_mut(4).zip(sample.values) {
                bytes.copy_from_slice(&value.to_le_bytes());
            }
            let payload = &payload[..len - RECORD_HEADER];
            let mut tag = kind;
            if timed && sample.utc_offset_ms == UTC_UNAVAILABLE {
                tag |= NO_UTC;
            }
            if matches!(version, Version::Evidence) && !sample.capture_time {
                tag |= 0x10; // received timestamp; measurement age is unknown
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
    for (axis, bytes) in payload.chunks_exact(4).enumerate() {
        let bits = u32::from_le_bytes(bytes.try_into().unwrap());
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
    use std::time::Instant;
    use std::{sync::mpsc, thread, time::Duration};

    fn fix(sequence: u64) -> Measurement {
        Measurement::Gnss(gnss::GnssFix {
            generation: 1,
            source_epoch_ms: 0,
            sequence,
            timestamp_us: sequence * 500_000,
            received_us: sequence * 500_000,
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
    fn sensor_fault_wakes_each_joined_owner_while_the_queue_is_locked() {
        let capture = Capture::default();
        for _ in 0..2 {
            thread::scope(|scope| {
                let (registered, attached) = mpsc::channel();
                let (notified, observed) = mpsc::channel();
                let capture = &capture;
                scope.spawn(move || {
                    capture.register_owner();
                    registered.send(()).unwrap();
                    let deadline = Instant::now() + Duration::from_secs(2);
                    while Instant::now() < deadline {
                        thread::park_timeout(deadline.saturating_duration_since(Instant::now()));
                        if capture.take_sensor_failed() {
                            notified.send(()).unwrap();
                            break;
                        }
                    }
                });
                attached.recv_timeout(Duration::from_secs(1)).unwrap();
                let result = {
                    let _queue = capture.samples.lock().unwrap();
                    scope.spawn(move || capture.sensor_failed());
                    observed.recv_timeout(Duration::from_secs(1))
                };
                assert_eq!(result, Ok(()));
            });
        }
    }

    #[test]
    fn skipped_wake_retains_fault_and_batch_readiness() {
        let capture = Capture::default();
        capture.register_owner();
        capture.enable().unwrap();
        thread::scope(|scope| {
            let (finished, complete) = mpsc::channel();
            let capture = &capture;
            let notified = {
                let _owner = capture.owner.get().unwrap().lock().unwrap();
                scope.spawn(move || {
                    capture.sensor_failed();
                    for sequence in 0..BATCH as u64 {
                        capture.record(fix(sequence));
                    }
                    finished.send(()).unwrap();
                });
                complete.recv_timeout(Duration::from_secs(1))
            };
            assert_eq!(notified, Ok(()));
        });
        assert!(capture.take_sensor_failed());
        assert!(!capture.take_sensor_failed());
        assert!(capture.take_ready());
        assert!(!capture.take_ready());
        assert_eq!(capture.finish(), BATCH);
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
                    received_us: time * 1_000 + 700,
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
        fix.received_us = fix.timestamp_us;
        fix.is_capture_time = true;
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
        for (axis, slot) in payload.chunks_exact_mut(4).enumerate() {
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
                    received_us: timestamp_ms * 1_000,
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

    fn imu(sequence: u64, time_ms: u64, capture_time: bool) -> Measurement {
        Measurement::Imu(
            0,
            imu::ImuSample {
                sequence,
                timestamp_us: time_ms * 1_000,
                received_us: time_ms * 1_000,
                is_capture_time: capture_time,
                sample_interval_us: 10_000,
                accel_mps2: [1.25, -0.5, 9.75],
                gyro_rps: [0.0, 1.0, -2.0],
            },
        )
    }

    fn qualified_fix(sequence: u64, time_ms: u64, utc_ms: i64) -> Measurement {
        let Measurement::Gnss(mut fix) = fix(sequence) else {
            unreachable!()
        };
        fix.timestamp_us = time_ms * 1_000;
        fix.received_us = fix.timestamp_us;
        fix.is_capture_time = true;
        fix.receiver_utc_ms = Some(utc_ms);
        Measurement::Gnss(fix)
    }

    #[test]
    fn source_failure_retains_the_prefix_and_first_cause_independent_of_queue_capacity() {
        let capture = Capture::default();
        capture.enable().unwrap();
        for sequence in 0..QUEUE as u64 {
            capture.record(fix(sequence));
        }
        capture.source_failed(SensorFault::Imu(0, imu::DeviceError::Timeout));
        capture.source_failed(SensorFault::Gnss(gnss::DeviceError::Io));
        capture.record(fix(QUEUE as u64));
        let mut bytes = [0; MAX_BYTES];
        for _ in (0..QUEUE).step_by(BATCH) {
            assert!(
                !capture
                    .take(&mut bytes, false, Version::Evidence)
                    .unwrap()
                    .terminal
            );
        }
        let fault = capture.take(&mut bytes, false, Version::Evidence).unwrap();
        assert!(fault.terminal);
        assert_eq!(&bytes[..fault.len], &[0xa5, 4, 2, 0, 1, 3]);
        let legacy = capture.take(&mut bytes, false, Version::Timed).unwrap();
        assert!(legacy.terminal);
        assert_eq!(legacy.len, 0);
        capture.finish();
        assert_eq!(
            capture.enable().unwrap_err().kind(),
            io::ErrorKind::NotConnected
        );
    }

    #[test]
    fn a_preexisting_failure_is_reportable_without_admitting_another_capture() {
        let capture = Capture::default();
        capture.source_failed(SensorFault::Imu(1, imu::DeviceError::Busy));
        assert!(capture.enable().is_err());
        assert!(capture.is_failed());
        capture.record(imu(1, 1_000, true));
        let mut bytes = [0; MAX_BYTES];
        let fault = capture.take(&mut bytes, true, Version::Evidence).unwrap();
        assert!(fault.terminal);
        assert_eq!(&bytes[..fault.len], &[0xa5, 4, 2, 0, 2, 2]);
        assert_eq!(capture.samples.lock().unwrap().queue.len(), 0);
    }

    #[test]
    fn no_fix_has_an_explicit_v4_record_and_does_not_stop_other_sources() {
        let capture = Capture::default();
        capture.enable().unwrap();
        capture.gnss_unavailable(gnss::NoFix {
            generation: 7,
            received_us: 1_000_000,
            source_epoch_ms: Some(500),
        });
        let mut bytes = [0; MAX_BYTES];
        let frame = capture.take(&mut bytes, true, Version::Evidence).unwrap();
        assert!(!frame.terminal);
        // Shared protocol golden: no mapping, generation 7, receipt 1000 ms,
        // receiver epoch 500 ms. NoFix has no timestamp delta or sample flags.
        assert_eq!(
            &bytes[..frame.len],
            &[
                0xa5, 4, 29, 0, 0, 0, 0, 0, 0, 0, 0, 128, 4, 7, 0, 0, 0, 232, 3, 0, 0, 0, 0, 0, 0,
                244, 1, 0, 0, 0, 0, 0, 0,
            ]
        );
        capture.record(imu(9, 1_000, false));
        let frame = capture.take(&mut bytes, true, Version::Evidence).unwrap();
        assert_eq!(frame.len, 49);
        assert_eq!(bytes[12], 1 | NO_UTC | 0x10);
        assert_eq!(&bytes[13..17], &9_u32.to_le_bytes());
        assert!(!frame.terminal);
    }

    #[test]
    fn legacy_export_skips_no_fix_without_fabricating_an_overflow() {
        for version in [Version::Fixed, Version::Compact, Version::Timed] {
            let capture = Capture::default();
            capture.enable().unwrap();
            capture.gnss_unavailable(gnss::NoFix {
                generation: 7,
                received_us: 1_000_000,
                source_epoch_ms: None,
            });
            capture.record(imu(1, 1_000, true));
            let mut bytes = [0; MAX_BYTES];
            let frame = capture.take(&mut bytes, true, version).unwrap();
            assert!(!frame.terminal);
            let start = if matches!(version, Version::Timed) {
                12
            } else {
                4
            };
            assert_eq!(bytes[start] & 0x0f, 1);
            assert!(capture.take(&mut bytes, true, version).is_none());
        }
    }

    #[test]
    fn losing_availability_evidence_is_not_a_fake_missing_sample_sequence() {
        let capture = Capture::default();
        capture.enable().unwrap();
        for sequence in 0..QUEUE as u64 {
            capture.record(fix(sequence));
        }
        capture.gnss_unavailable(gnss::NoFix {
            generation: 7,
            received_us: 1_000_000,
            source_epoch_ms: None,
        });
        let mut bytes = [0; MAX_BYTES];
        for _ in (0..QUEUE).step_by(BATCH) {
            assert!(
                !capture
                    .take(&mut bytes, false, Version::Evidence)
                    .unwrap()
                    .terminal
            );
        }
        let fault = capture.take(&mut bytes, false, Version::Evidence).unwrap();
        assert!(fault.terminal);
        assert_eq!(&bytes[..fault.len], &[0xa5, 4, 2, 0, 3, 7]);
    }

    #[test]
    fn queued_samples_keep_their_mapping_across_clock_replacement_and_no_fix() {
        let capture = Capture::default();
        capture.record(qualified_fix(1, 1_000, 100_000));
        capture.enable().unwrap();
        capture.record(imu(1, 1_500, true));
        // A delayed fix changes the mapping at a capture time before the IMU.
        capture.record(qualified_fix(2, 1_400, 200_000));
        capture.gnss_unavailable(gnss::NoFix {
            generation: 1,
            received_us: 1_600_000,
            source_epoch_ms: Some(2_000),
        });
        assert!(capture.clock().is_none());
        let mut bytes = [0; MAX_BYTES];
        let first = capture.take(&mut bytes, true, Version::Evidence).unwrap();
        assert_eq!(first.len, 49);
        assert_eq!(&bytes[4..12], &99_000_i64.to_le_bytes());
        assert_eq!(bytes[12], 1);
        let second = capture.take(&mut bytes, true, Version::Evidence).unwrap();
        assert_eq!(second.len, 12 + 33 + 21);
        assert_eq!(&bytes[4..12], &198_600_i64.to_le_bytes());
        assert_eq!(bytes[12], 3);
        assert_eq!(bytes[45], 4);
    }

    #[test]
    fn received_and_expired_measurements_never_gain_utc_from_a_current_mapping() {
        let capture = Capture::default();
        capture.record(qualified_fix(1, 1_000, 100_000));
        capture.enable().unwrap();
        capture.record(imu(1, 1_500, false));
        capture.record(imu(2, 4_000, true));
        let mut bytes = [0; MAX_BYTES];
        let frame = capture.take(&mut bytes, true, Version::Evidence).unwrap();
        assert_eq!(frame.len, 12 + 37 + 37);
        assert_eq!(&bytes[4..12], &UTC_UNAVAILABLE.to_le_bytes());
        assert_eq!(bytes[12], 1 | NO_UTC | 0x10);
        assert_eq!(bytes[49], 1 | NO_UTC);
    }

    #[test]
    fn capture_generation_exhaustion_never_reuses_an_old_epoch() {
        let capture = Capture::default();
        capture.epoch.store(FAILED - 3, Ordering::Relaxed);
        assert!(capture.enable().is_err());
        assert_eq!(capture.epoch.load(Ordering::Acquire), FAILED - 3);
    }
}
