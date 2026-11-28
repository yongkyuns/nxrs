//! Bounded capture for the authenticated navigation telemetry export stream.
//!
//! Three exclusive acquisition publishers share a fixed scalar pool. The
//! communications owner copies complete batches before encoding or transport I/O.
#[path = "export/policy.rs"]
mod policy;
#[cfg(feature = "probe")]
pub mod probe;
pub use policy::CaptureBudget;

use crate::clock::ClockAnchor;
use messaging::pooled::{self, Notify, StartError, Status};
use navigation::observations::Measurement;
use std::io;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex, OnceLock,
};
use std::thread::{self, Thread};
use std::time::Duration;

pub const REQUEST_PREFIX: &[u8; 4] = b"NXRS";
pub const REQUEST_LEN: usize = REQUEST_PREFIX.len() + 1;
const BATCH: usize = 16;
const RECORD_HEADER: usize = 13;
const IMU_BYTES: usize = RECORD_HEADER + 24;
const GNSS_BYTES: usize = RECORD_HEADER + 20;
const UTC_UNAVAILABLE: i64 = i64::MIN;
const DELTA: u8 = 0x80;
const COUNTS: u8 = 0x40;
const NO_UTC: u8 = 0x20;
pub const MAX_BYTES: usize = 4 + 8 + BATCH * IMU_BYTES;

/// Admission and retained-storage choices supplied by the application.
/// Rates are in millihertz; an absent IMU or zero GNSS rate contributes no
/// assumed traffic to the admission budget.
/// The caller connects only the corresponding configured source callbacks;
/// these declarations do not impose per-sample rate limits or disable a lane.
/// Actual traffic exceeding the fixed pool still terminates export on overflow.
pub struct CaptureConfig {
    pub scales: [Option<[u32; 2]>; 2],
    pub imu_rates_millihz: [Option<u32>; 2],
    pub gnss_rate_millihz: u32,
    pub reserve_export: bool,
    pub budget: CaptureBudget,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
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

/// Read-only product state remains useful when export storage is not reserved.
struct CaptureShared {
    sensor_failed: AtomicBool,
    owner: OnceLock<Mutex<Thread>>,
    scales: [Option<[u32; 2]>; 2],
    clock: OnceLock<ClockAnchor>,
}

struct CaptureWake(Arc<CaptureShared>);

impl Notify for CaptureWake {
    fn notify(&self) {
        self.0.notify_owner();
    }
}

/// The sole pool consumer moves with its caller through spawn and joined handoff.
pub struct Capture {
    owner: Option<pooled::Owner<CaptureWake>>,
    shared: Arc<CaptureShared>,
    retiring: bool,
}

/// A source-owned callback endpoint. It is neither Clone nor Sync.
pub struct CapturePublisher {
    publisher: Option<pooled::Publisher<CaptureWake>>,
    source: u8,
    shared: Arc<CaptureShared>,
}

#[derive(Clone)]
pub struct CaptureView(Arc<CaptureShared>);

impl CaptureView {
    pub fn clock(&self) -> Option<ClockAnchor> {
        self.0.clock.get().copied()
    }
}

impl CaptureShared {
    fn register_owner(&self) {
        // Only the joined owner may replace this destination. Initialize and
        // prewarm native mutex storage on that worker before publishing it.
        let current = thread::current();
        let owner = self.owner.get_or_init(|| {
            let owner = Mutex::new(current.clone());
            drop(owner.lock().unwrap());
            owner
        });
        let retired = std::mem::replace(&mut *owner.lock().unwrap(), current);
        drop(retired);
    }

    fn notify_owner(&self) {
        // Publication/fault state precedes this hint. No acquisition callback
        // waits for replacement or clones/retires a Thread. Active work and
        // pending cancellation retain a bounded owner polling deadline.
        if let Some(owner) = self.owner.get() {
            if let Ok(owner) = owner.try_lock() {
                owner.unpark();
            }
        }
    }
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

impl From<pooled::Captured> for Sample {
    fn from(sample: pooled::Captured) -> Self {
        Self {
            time: u64::from(sample.record.words[0]) | (u64::from(sample.record.words[1]) << 32),
            sequence: sample.record.sequence,
            source: match sample.source {
                0 | 1 => Source::Imu(sample.source),
                2 => Source::Gnss,
                _ => unreachable!("unconfigured capture source"),
            },
            values: sample.record.words[2..].try_into().unwrap(),
        }
    }
}

impl Sample {
    fn record(self) -> pooled::Record {
        pooled::Record {
            sequence: self.sequence,
            words: [
                self.time as u32,
                (self.time >> 32) as u32,
                self.values[0],
                self.values[1],
                self.values[2],
                self.values[3],
                self.values[4],
                self.values[5],
            ],
        }
    }
}

impl CapturePublisher {
    /// Publish one normalized measurement on its configured source endpoint.
    ///
    /// # Panics
    ///
    /// Panics if an IMU ordinal is outside the protocol's two IMU lanes or the
    /// measurement belongs to a different endpoint. Source wiring is fixed at
    /// composition time; the GNSS lane cannot accept IMU measurements.
    pub fn record(&mut self, measurement: Measurement) {
        // Capture the epoch before scalar conversion so a paused callback
        // cannot submit old work after a reconnect opens a different epoch.
        let epoch = self
            .publisher
            .as_ref()
            .map(pooled::Publisher::captured_epoch);
        let source = match measurement {
            Measurement::Imu(index, _) => {
                assert!(index < 2, "capture supports only IMU lanes 0 and 1");
                index
            }
            Measurement::Gnss(_) => 2,
        };
        assert_eq!(source, usize::from(self.source), "capture source mismatch");
        let sample = Sample::from(measurement);
        if let Measurement::Gnss(fix) = measurement {
            if let Some(anchor) = fix
                .receiver_utc_ms
                .and_then(|utc| ClockAnchor::new(sample.time, utc))
            {
                let _ = self.shared.clock.set(anchor);
            }
        }
        if let (Some(publisher), Some(epoch)) = (self.publisher.as_mut(), epoch) {
            // Closed is normal outside an export. Full retains the exact first
            // missing key in the pool; the owner emits it after admitted work.
            let _ = publisher.offer(epoch, sample.record());
        }
    }

    pub fn sensor_failed(&self) {
        self.shared.sensor_failed.store(true, Ordering::Release);
        self.shared.notify_owner();
    }
}

impl Capture {
    /// Reserve before starting acquisition. Enabled export retains its bounded
    /// pool in active and idle epochs until the last owner/publisher is dropped.
    /// Scalar arenas use fallible reservation; Arc control allocation follows
    /// Rust's ordinary infallible allocation contract.
    pub fn new(config: CaptureConfig) -> io::Result<(Self, [CapturePublisher; pooled::SOURCES])> {
        if config.reserve_export {
            config
                .budget
                .validate(config.imu_rates_millihz, config.gnss_rate_millihz)?;
        }
        let shared = Arc::new(CaptureShared {
            sensor_failed: AtomicBool::new(false),
            owner: OnceLock::new(),
            scales: config.scales,
            clock: OnceLock::new(),
        });
        let (owner, publishers) = if config.reserve_export {
            let (owner, publishers) = pooled::channel(
                pooled::RetainedBudget {
                    max_requested_bytes: config.budget.max_requested_bytes,
                },
                CaptureWake(shared.clone()),
            )
            .map_err(|error| match error {
                pooled::SetupError::Allocation(_) => io::ErrorKind::OutOfMemory.into(),
                pooled::SetupError::BudgetExceeded { required, maximum } => io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("capture reservation requires {required} bytes, budget is {maximum}"),
                ),
            })?;
            (Some(owner), publishers.map(Some))
        } else {
            (None, [None, None, None])
        };
        let mut source = 0;
        let publishers = publishers.map(|publisher| {
            let endpoint = CapturePublisher {
                publisher,
                source,
                shared: shared.clone(),
            };
            source += 1;
            endpoint
        });
        Ok((
            Self {
                owner,
                shared,
                retiring: false,
            },
            publishers,
        ))
    }

    pub fn view(&self) -> CaptureView {
        CaptureView(self.shared.clone())
    }

    pub fn scales(&self) -> [Option<[u32; 2]>; 2] {
        self.shared.scales
    }

    pub fn clock(&self) -> Option<ClockAnchor> {
        self.shared.clock.get().copied()
    }

    pub fn register_owner(&self) {
        self.shared.register_owner();
    }

    pub fn take_ready(&mut self) -> bool {
        self.owner.as_mut().is_some_and(pooled::Owner::take_ready)
    }

    pub fn take_sensor_failed(&self) -> bool {
        self.shared.sensor_failed.swap(false, Ordering::AcqRel)
    }

    /// False means an earlier cancellation is still retiring. The request
    /// remains pending in the radio session; other communications keeps running.
    pub fn enable(&mut self) -> io::Result<bool> {
        if self.retire() {
            return Ok(false);
        }
        let owner = self.owner.as_mut().ok_or(io::ErrorKind::Unsupported)?;
        match owner.begin() {
            Ok(_) => Ok(true),
            Err(StartError::Pending) => Ok(false),
            Err(StartError::Disconnected) => Err(io::ErrorKind::NotConnected.into()),
            Err(StartError::Exhausted) => Err(io::Error::other("capture epoch budget exhausted")),
        }
    }

    pub fn finish(&mut self) -> usize {
        let Some(owner) = &mut self.owner else {
            return 0;
        };
        let high_water = owner.high_water();
        if owner.status() != Status::Inactive {
            owner.cancel();
            self.retiring = true;
            self.retire();
        }
        high_water
    }

    /// Drain only published work without waiting for a preempted producer.
    /// The caller includes a bounded deadline while this returns true.
    pub fn retire(&mut self) -> bool {
        if !self.retiring {
            return false;
        }
        let owner = self.owner.as_mut().unwrap();
        for _ in 0..pooled::CAPACITY {
            if owner.poll().is_none() {
                break;
            }
        }
        if matches!(owner.status(), Status::Overflow(_) | Status::Cancelled) {
            owner.finish().expect("quiescent capture retirement");
            self.retiring = false;
        }
        self.retiring
    }

    pub fn retirement_pending(&self) -> bool {
        self.retiring
    }

    pub fn take(
        &mut self,
        output: &mut [u8; MAX_BYTES],
        due: bool,
        version: Version,
    ) -> io::Result<Option<Batch>> {
        if self.retiring {
            return Ok(None);
        }
        let Some(owner) = self.owner.as_mut() else {
            return Ok(None);
        };
        let count = owner.published_len().min(BATCH);
        let status = owner.status();
        // Per-source FIFO is retained; the owner fairly interleaves independent
        // sources. No record waits behind an unpublished slot in another lane.
        if count < BATCH && !due && matches!(status, Status::Open | Status::Inactive) {
            return Ok(None);
        }
        if count == 0 {
            if status == Status::Cancelled {
                // A vanished or unwinding producer cannot leave a successful
                // export apparently live forever. No missing key is invented.
                return Err(io::ErrorKind::BrokenPipe.into());
            }
            let Status::Overflow(missing) = status else {
                return Ok(None);
            };
            let len = match version {
                Version::Fixed => 4,
                Version::Compact | Version::Timed => {
                    output[4] = missing.source + 1;
                    output[5..9].copy_from_slice(&missing.sequence.to_le_bytes());
                    9
                }
            };
            header(output, version, len);
            return Ok(Some(Batch {
                len,
                terminal: true,
            }));
        }
        let mut records = [None::<Sample>; BATCH];
        for record in &mut records[..count] {
            *record = Some(owner.poll().expect("published capture record").into());
        }

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
            for (bytes, value) in payload.chunks_exact_mut(4).zip(sample.values) {
                bytes.copy_from_slice(&value.to_le_bytes());
            }
            let payload = &payload[..len - RECORD_HEADER];
            let mut tag = kind;
            if timed && clock.is_some_and(|anchor| time < anchor.since_ms) {
                tag |= NO_UTC;
            }
            let counts = if timed && kind != 3 {
                self.shared.scales[index].and_then(|scale| encode_counts(payload, scale))
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
        Ok(Some(Batch {
            len: end,
            terminal: false,
        }))
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
pub(crate) mod tests {
    use super::*;
    use messaging::pooled::Stage;
    use std::sync::mpsc;
    use std::time::Instant;

    pub(crate) fn fixture(scales: [Option<[u32; 2]>; 2]) -> (Capture, [CapturePublisher; 3]) {
        Capture::new(config(scales, [Some(100_000); 2], true)).unwrap()
    }

    pub(crate) fn config(
        scales: [Option<[u32; 2]>; 2],
        imu_rates_millihz: [Option<u32>; 2],
        reserve_export: bool,
    ) -> CaptureConfig {
        CaptureConfig {
            scales,
            imu_rates_millihz,
            gnss_rate_millihz: 2_000,
            reserve_export,
            budget: CaptureBudget {
                max_requested_bytes: 22 * 1024,
                stall_ms: 2_000,
                burst_records: 64,
            },
        }
    }

    pub(crate) fn fix(sequence: u64) -> Measurement {
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

    pub(crate) fn imu(source: usize, sequence: u64, time: u64) -> Measurement {
        Measurement::Imu(
            source,
            imu::ImuSample {
                sequence,
                timestamp_us: time * 1_000 + 700,
                is_capture_time: true,
                sample_interval_us: 10_000,
                accel_mps2: [-4096.0, 0.0, 4095.875],
                gyro_rps: [-0.25, 1.0, 8191.75],
            },
        )
    }

    #[cfg(feature = "ble")]
    mod paused;
    #[cfg(feature = "ble")]
    pub(crate) use paused::pause_record;

    #[derive(Debug, PartialEq)]
    pub(crate) struct Decoded {
        pub source: usize,
        pub sequence: u32,
        pub time: u64,
        pub utc: Option<i64>,
        pub values: [u32; 6],
    }

    /// Independent peer-side decode of the documented v1/v2/v3 wire layouts.
    pub(crate) fn decode(bytes: &[u8], scales: [Option<[u32; 2]>; 2]) -> Vec<Decoded> {
        assert_eq!(bytes[0], 0xa5);
        assert_eq!(
            usize::from(u16::from_le_bytes(bytes[2..4].try_into().unwrap())),
            bytes.len() - 4
        );
        let timed = bytes[1] == 3;
        let mut pos = 4;
        let offset = if timed {
            pos += 8;
            i64::from_le_bytes(bytes[4..12].try_into().unwrap())
        } else {
            UTC_UNAVAILABLE
        };
        let mut previous = [None::<(u32, u64)>; 3];
        let mut result = Vec::new();
        while pos < bytes.len() {
            let tag = bytes[pos];
            let source = usize::from(tag & if timed { 0x1f } else { 0x7f }) - 1;
            assert!(source < 3);
            let delta = bytes[1] >= 2 && tag & DELTA != 0;
            let counts = timed && tag & COUNTS != 0;
            let (sequence, time) = if delta {
                let (sequence, time) = previous[source].unwrap();
                let time = time + u64::from(bytes[pos + 1]);
                pos += 2;
                (
                    sequence.wrapping_add(1),
                    if timed { time } else { u64::from(time as u32) },
                )
            } else {
                let sequence = u32::from_le_bytes(bytes[pos + 1..pos + 5].try_into().unwrap());
                let time = if timed {
                    u64::from_le_bytes(bytes[pos + 5..pos + 13].try_into().unwrap())
                } else {
                    u64::from(u32::from_le_bytes(
                        bytes[pos + 5..pos + 9].try_into().unwrap(),
                    ))
                };
                pos += if timed { 13 } else { 9 };
                (sequence, time)
            };
            previous[source] = Some((sequence, time));
            let mut values = [0; 6];
            if counts {
                assert!(source < 2);
                let scale = scales[source].unwrap().map(f32::from_bits);
                for (axis, value) in values.iter_mut().enumerate() {
                    let point = i16::from_le_bytes(bytes[pos..pos + 2].try_into().unwrap());
                    *value = (f32::from(point) * scale[axis / 3]).to_bits();
                    pos += 2;
                }
            } else {
                let words = if source == 2 { 5 } else { 6 };
                for value in &mut values[..words] {
                    *value = u32::from_le_bytes(bytes[pos..pos + 4].try_into().unwrap());
                    pos += 4;
                }
            }
            result.push(Decoded {
                source,
                sequence,
                time,
                values,
                utc: (timed && offset != UTC_UNAVAILABLE && tag & NO_UTC == 0)
                    .then(|| i64::try_from(time).unwrap() + offset),
            });
        }
        assert_eq!(pos, bytes.len());
        result
    }

    #[test]
    fn short_batches_keep_pool_credits_until_the_deadline() {
        let (mut capture, [_, _, mut gnss]) = fixture([None; 2]);
        capture.enable().unwrap();
        gnss.record(fix(1));
        let mut bytes = [0; MAX_BYTES];
        assert!(capture
            .take(&mut bytes, false, Version::Fixed)
            .unwrap()
            .is_none());
        assert_eq!(capture.owner.as_ref().unwrap().obligations().0, 1);
        let batch = capture
            .take(&mut bytes, true, Version::Fixed)
            .unwrap()
            .unwrap();
        assert_eq!(batch.len, 33);
        assert_eq!(decode(&bytes[..batch.len], [None; 2])[0].sequence, 1);
        assert_eq!(capture.owner.as_ref().unwrap().obligations().0, 0);
    }

    #[test]
    fn sensor_fault_and_other_sources_progress_past_an_unpublished_slot() {
        let (mut capture, [mut primary, _, mut gnss]) = fixture([None; 2]);
        capture.enable().unwrap();
        let (claimed, observed) = mpsc::channel();
        let (resume, released) = mpsc::channel();
        let epoch = primary.publisher.as_ref().unwrap().captured_epoch();
        let worker = thread::spawn(move || {
            primary
                .publisher
                .as_mut()
                .unwrap()
                .offer_with_hooks(epoch, Sample::from(imu(0, 1, 10)).record(), |stage| {
                    if stage == Stage::AfterSlotClaim {
                        claimed.send(()).unwrap();
                        released.recv().unwrap();
                    }
                })
                .unwrap();
        });
        observed.recv_timeout(Duration::from_secs(2)).unwrap();
        gnss.sensor_failed();
        gnss.record(fix(1));
        assert!(capture.take_sensor_failed());
        let mut bytes = [0; MAX_BYTES];
        let batch = capture
            .take(&mut bytes, true, Version::Fixed)
            .unwrap()
            .unwrap();
        assert_eq!(decode(&bytes[..batch.len], [None; 2])[0].source, 2);
        assert_eq!(capture.finish(), 2);
        assert!(capture.retirement_pending());
        assert!(!capture.enable().unwrap());
        resume.send(()).unwrap();
        worker.join().unwrap();
        assert!(!capture.retire());
        assert!(capture.enable().unwrap());
        gnss.record(fix(2));
        let batch = capture
            .take(&mut bytes, true, Version::Fixed)
            .unwrap()
            .unwrap();
        assert_eq!(decode(&bytes[..batch.len], [None; 2])[0].sequence, 2);
    }

    #[test]
    fn joined_owner_replacement_wakes_the_new_worker() {
        let (capture, [_, _, gnss]) = fixture([None; 2]);
        let mut previous = None;
        for _ in 0..2 {
            let shared = capture.shared.clone();
            let (registered, attached) = mpsc::channel();
            let (notified, observed) = mpsc::channel();
            let worker = thread::spawn(move || {
                shared.register_owner();
                registered.send(thread::current()).unwrap();
                let deadline = Instant::now() + Duration::from_secs(2);
                loop {
                    thread::park_timeout(deadline.saturating_duration_since(Instant::now()));
                    if shared.sensor_failed.swap(false, Ordering::AcqRel) {
                        notified.send(()).unwrap();
                        break;
                    }
                    if Instant::now() >= deadline {
                        break;
                    }
                }
            });
            let owner = attached.recv_timeout(Duration::from_secs(1)).unwrap();
            assert_ne!(previous, Some(owner.id()));
            assert_eq!(
                capture.shared.owner.get().unwrap().lock().unwrap().id(),
                owner.id()
            );
            gnss.sensor_failed();
            let result = observed.recv_timeout(Duration::from_secs(1));
            owner.unpark();
            worker.join().unwrap();
            assert_eq!(result, Ok(()));
            previous = Some(owner.id());
        }
    }

    #[test]
    fn skipped_wakes_retain_fault_and_batch_readiness_without_waiting() {
        let (mut capture, [_, _, mut gnss]) = fixture([None; 2]);
        capture.register_owner();
        capture.enable().unwrap();
        let shared = capture.shared.clone();
        let (finished, complete) = mpsc::channel();
        let owner = shared.owner.get().unwrap().lock().unwrap();
        let worker = thread::spawn(move || {
            gnss.sensor_failed();
            for sequence in 0..BATCH {
                gnss.record(fix(sequence as u64));
            }
            finished.send(()).unwrap();
            gnss
        });
        let result = complete.recv_timeout(Duration::from_secs(1));
        drop(owner);
        let _gnss = worker.join().unwrap();
        assert_eq!(result, Ok(()));
        assert!(capture.take_sensor_failed());
        assert!(!capture.take_sensor_failed());
        assert!(capture.take_ready());
        assert!(!capture.take_ready());
        assert_eq!(capture.finish(), BATCH);
    }

    #[test]
    fn overflow_drains_admitted_records_and_retains_the_exact_terminal() {
        let (mut capture, [_, _, mut gnss]) = fixture([None; 2]);
        capture.enable().unwrap();
        for sequence in 0..=(pooled::CAPACITY + 1) {
            gnss.record(fix(sequence as u64));
        }
        let mut bytes = [0; MAX_BYTES];
        for first in (0..pooled::CAPACITY).step_by(BATCH) {
            let batch = capture
                .take(&mut bytes, false, Version::Fixed)
                .unwrap()
                .unwrap();
            assert!(!batch.terminal);
            assert_eq!(batch.len, 4 + BATCH * 29);
            assert_eq!(&bytes[5..9], &(first as u32).to_le_bytes());
        }
        for version in [Version::Compact, Version::Timed, Version::Fixed] {
            let failure = capture.take(&mut bytes, false, version).unwrap().unwrap();
            assert!(failure.terminal);
            if version == Version::Fixed {
                assert_eq!(&bytes[..failure.len], &[0xa5, 1, 0, 0]);
            } else {
                assert_eq!(
                    &bytes[..failure.len],
                    &[0xa5, version as u8, 5, 0, 3, 0, 2, 0, 0]
                );
            }
        }
        capture.finish();
        assert!(capture.enable().unwrap());
        assert!(capture
            .take(&mut bytes, true, Version::Fixed)
            .unwrap()
            .is_none());
    }

    #[test]
    fn reconnect_rejects_work_captured_in_the_previous_epoch() {
        let (mut capture, [_, _, mut gnss]) = fixture([None; 2]);
        capture.enable().unwrap();
        let old_epoch = gnss.publisher.as_ref().unwrap().captured_epoch();
        gnss.record(fix(1));
        assert_eq!(capture.finish(), 1);
        assert!(capture.enable().unwrap());
        let rejected = gnss
            .publisher
            .as_mut()
            .unwrap()
            .offer(old_epoch, Sample::from(fix(2)).record())
            .unwrap_err();
        assert_eq!(rejected.reason, pooled::Rejection::Closed);
        gnss.record(fix(3));
        let mut bytes = [0; MAX_BYTES];
        let batch = capture
            .take(&mut bytes, true, Version::Fixed)
            .unwrap()
            .unwrap();
        assert_eq!(decode(&bytes[..batch.len], [None; 2])[0].sequence, 3);
    }

    #[test]
    fn source_retirement_drains_records_then_reports_a_failed_export() {
        let (mut capture, [a, b, mut gnss]) = fixture([None; 2]);
        capture.enable().unwrap();
        gnss.record(fix(1));
        drop((a, b, gnss));
        let mut bytes = [0; MAX_BYTES];
        assert!(
            !capture
                .take(&mut bytes, false, Version::Timed)
                .unwrap()
                .unwrap()
                .terminal
        );
        assert_eq!(
            capture
                .take(&mut bytes, true, Version::Timed)
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::BrokenPipe
        );
        capture.finish();
        assert_eq!(
            capture.enable().err().unwrap().kind(),
            io::ErrorKind::NotConnected
        );
    }

    #[test]
    fn no_export_reservation_still_publishes_clock_and_faults() {
        let (mut capture, [_, _, mut gnss]) =
            Capture::new(config([None; 2], [None; 2], false)).unwrap();
        assert!(capture.owner.is_none());
        let view = capture.view();
        let Measurement::Gnss(mut point) = fix(1) else {
            unreachable!()
        };
        point.receiver_utc_ms = Some(1_767_225_600_000);
        gnss.record(Measurement::Gnss(point));
        assert_eq!(view.clock().unwrap().utc_ms(500), point.receiver_utc_ms);
        gnss.sensor_failed();
        assert!(capture.take_sensor_failed());
        assert_eq!(
            capture.enable().err().unwrap().kind(),
            io::ErrorKind::Unsupported
        );
    }

    #[test]
    fn all_wire_versions_preserve_source_order_wraps_gaps_and_scalar_bits() {
        let scale = [0.125_f32.to_bits(), 0.25_f32.to_bits()];
        let scales = [Some(scale), None];
        let origin = u64::from(u32::MAX) - 2;
        for version in [Version::Fixed, Version::Compact, Version::Timed] {
            let (mut capture, mut publishers) = fixture(scales);
            let Measurement::Gnss(mut anchor) = fix(0) else {
                unreachable!()
            };
            anchor.timestamp_us = (origin + 3) * 1_000;
            anchor.receiver_utc_ms = Some(1_767_225_600_000);
            publishers[2].record(Measurement::Gnss(anchor));
            capture.enable().unwrap();
            let mut expected: [Vec<Decoded>; 3] = std::array::from_fn(|_| Vec::new());
            for (sequence, elapsed) in [(u32::MAX, 0), (0, 9), (2, 300)] {
                for source in (0..3).rev() {
                    let time = origin + elapsed + source as u64;
                    let measurement = if source < 2 {
                        let mut value = imu(source, u64::from(sequence), time);
                        if let Measurement::Imu(1, point) = &mut value {
                            point.accel_mps2 = [-0.0, f32::from_bits(0x7fc01234), 0.1];
                        }
                        value
                    } else {
                        let Measurement::Gnss(mut point) = fix(0) else {
                            unreachable!()
                        };
                        point.timestamp_us = time * 1_000;
                        point.sequence = u64::from(sequence);
                        Measurement::Gnss(point)
                    };
                    let sample = Sample::from(measurement);
                    publishers[source].record(measurement);
                    expected[source].push(Decoded {
                        source,
                        sequence,
                        time: if version == Version::Timed {
                            time
                        } else {
                            u64::from(time as u32)
                        },
                        utc: (version == Version::Timed && time >= origin + 3)
                            .then(|| 1_767_225_600_000 + time as i64 - (origin + 3) as i64),
                        values: sample.values,
                    });
                }
            }
            let mut bytes = [0; MAX_BYTES];
            let batch = capture.take(&mut bytes, true, version).unwrap().unwrap();
            let mut actual: [Vec<Decoded>; 3] = std::array::from_fn(|_| Vec::new());
            for record in decode(&bytes[..batch.len], scales) {
                actual[record.source].push(record);
            }
            assert_eq!(actual, expected);
        }
    }

    #[test]
    fn counts_are_bit_exact_or_keep_float_records() {
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
    #[should_panic(expected = "capture supports only IMU lanes 0 and 1")]
    fn oversized_source_ordinal_cannot_alias_a_configured_lane() {
        let (_, [mut primary, _, _]) = fixture([None; 2]);
        primary.record(imu(256, 1, 10));
    }

    #[test]
    #[should_panic(expected = "capture source mismatch")]
    fn publisher_rejects_a_measurement_from_another_source() {
        let (_, [mut primary, _, _]) = fixture([None; 2]);
        primary.record(fix(0));
    }

    #[test]
    #[should_panic(expected = "capture supports only IMU lanes 0 and 1")]
    fn third_imu_cannot_alias_the_gnss_protocol_lane() {
        let (_, [_, _, mut gnss]) = fixture([None; 2]);
        gnss.record(imu(2, 1, 10));
    }
}
