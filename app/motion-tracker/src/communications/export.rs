//! Bounded capture for the authenticated tracker export stream.
use navigation::observations::Measurement;
use std::collections::VecDeque;
use std::io;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex,
};
use std::thread::{self, Thread};

pub const REQUEST_PREFIX: &[u8; 4] = b"NXRS";
pub const REQUEST_LEN: usize = REQUEST_PREFIX.len() + 1;
// Roughly 2.5 seconds at two 100 Hz IMUs and 2 Hz GNSS. Retain samples
// while connection-level backpressure clears and the exporter catches up.
const QUEUE: usize = 512;
const BATCH: usize = 16;
const IMU_BYTES: usize = 33;
const GNSS_BYTES: usize = 29;
pub const MAX_BYTES: usize = 4 + BATCH * IMU_BYTES;

#[derive(Clone, Copy)]
pub enum Version {
    Fixed = 1,
    Compact = 2,
}

impl Version {
    pub fn from_request(value: u8) -> Option<Self> {
        match value {
            1 => Some(Self::Fixed),
            2 => Some(Self::Compact),
            _ => None,
        }
    }
}

pub struct Batch {
    pub len: usize,
    pub terminal: bool,
}

#[derive(Default)]
pub struct Capture {
    enabled: AtomicBool,
    ready: AtomicBool,
    samples: Mutex<Samples>,
}

#[derive(Default)]
struct Samples {
    queue: VecDeque<[u8; IMU_BYTES]>,
    first_missing: Option<[u8; 5]>,
    high_water: usize,
    owner: Option<Thread>,
}

impl Capture {
    pub fn register_owner(&self) {
        self.samples.lock().unwrap().owner = Some(thread::current());
    }

    pub fn take_ready(&self) -> bool {
        self.ready.swap(false, Ordering::AcqRel)
    }

    pub fn enable(&self) -> io::Result<()> {
        let mut samples = self.samples.lock().unwrap();
        samples.queue.clear();
        samples
            .queue
            .try_reserve_exact(QUEUE)
            .map_err(io::Error::other)?;
        samples.first_missing = None;
        samples.high_water = 0;
        self.ready.store(false, Ordering::Relaxed);
        self.enabled.store(true, Ordering::Release);
        Ok(())
    }

    pub fn disable(&self) {
        self.enabled.store(false, Ordering::Release);
    }

    pub fn high_water(&self) -> usize {
        self.samples.lock().unwrap().high_water
    }

    pub fn record(&self, sample: Measurement) {
        if !self.enabled.load(Ordering::Acquire) {
            return;
        }
        let bytes = encode(sample);
        // This runs in the acquisition task. The consumer holds the lock
        // only for a bounded copy, with no encoding or transport I/O.
        let mut samples = self.samples.lock().unwrap();
        if !self.enabled.load(Ordering::Relaxed) {
            return;
        }
        let ready = if samples.queue.len() == QUEUE {
            samples.first_missing = Some(bytes[..5].try_into().unwrap());
            self.disable();
            true
        } else {
            samples.queue.push_back(bytes);
            let depth = samples.queue.len();
            samples.high_water = samples.high_water.max(depth);
            depth == BATCH
        };
        if ready {
            self.ready.store(true, Ordering::Release);
            let owner = samples.owner.clone();
            drop(samples);
            if let Some(owner) = owner {
                owner.unpark();
            }
        }
    }

    pub fn take(&self, output: &mut [u8; MAX_BYTES], due: bool, version: Version) -> Option<Batch> {
        let mut records = [[0; IMU_BYTES]; BATCH];
        let count = {
            let mut samples = self.samples.lock().unwrap();
            let count = samples.queue.len().min(BATCH);
            // Full batches can catch up while the deadline bounds how
            // long a smaller batch waits to fill.
            if count < BATCH && !due && samples.first_missing.is_none() {
                return None;
            }
            if count == 0 {
                let missing = samples.first_missing?;
                let len = match version {
                    Version::Fixed => 4,
                    Version::Compact => {
                        output[4..9].copy_from_slice(&missing);
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
                *record = sample;
            }
            count
        };

        let mut previous = [None::<(u32, u32)>; 3];
        let mut end = 4;
        for sample in &records[..count] {
            let kind = sample[0];
            let len = if kind == 3 { GNSS_BYTES } else { IMU_BYTES };
            if matches!(version, Version::Compact) {
                let sequence = u32::from_le_bytes(sample[1..5].try_into().unwrap());
                let time = u32::from_le_bytes(sample[5..9].try_into().unwrap());
                let last = &mut previous[usize::from(kind - 1)];
                let delta = last
                    .filter(|(seq, _)| seq.wrapping_add(1) == sequence)
                    .and_then(|(_, stamp)| u8::try_from(time.wrapping_sub(stamp)).ok());
                *last = Some((sequence, time));
                if let Some(delta) = delta {
                    let payload = &sample[9..len];
                    output[end] = kind | 0x80;
                    output[end + 1] = delta;
                    end += 2;
                    output[end..end + payload.len()].copy_from_slice(payload);
                    end += payload.len();
                    continue;
                }
            }
            output[end..end + len].copy_from_slice(&sample[..len]);
            end += len;
        }
        header(output, version, end);
        Some(Batch {
            len: end,
            terminal: false,
        })
    }
}

fn header(output: &mut [u8; MAX_BYTES], version: Version, len: usize) {
    output[..2].copy_from_slice(&[0xa5, version as u8]);
    output[2..4].copy_from_slice(&((len - 4) as u16).to_le_bytes());
}

fn encode(sample: Measurement) -> [u8; IMU_BYTES] {
    let mut output = [0; IMU_BYTES];
    let (tag, sequence, time) = match sample {
        Measurement::Imu(index, sample) => (1 + index as u8, sample.sequence, sample.timestamp_ms),
        Measurement::Gnss(fix) => (3, fix.sequence, fix.timestamp_ms),
    };
    output[0] = tag;
    output[1..5].copy_from_slice(&(sequence as u32).to_le_bytes());
    output[5..9].copy_from_slice(&(time as u32).to_le_bytes());
    match sample {
        Measurement::Imu(_, sample) => {
            for (field, value) in output[9..IMU_BYTES]
                .chunks_exact_mut(4)
                .zip(sample.accel_mps2.into_iter().chain(sample.gyro_rps))
            {
                field.copy_from_slice(&value.to_le_bytes());
            }
        }
        Measurement::Gnss(fix) => {
            output[9..17].copy_from_slice(&fix.latitude_deg.to_le_bytes());
            output[17..25].copy_from_slice(&fix.longitude_deg.to_le_bytes());
            output[25..29].copy_from_slice(&fix.speed_mps.to_le_bytes());
        }
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{sync::mpsc, thread, time::Duration};

    fn fix(sequence: u64) -> Measurement {
        Measurement::Gnss(gnss::GnssFix {
            sequence,
            timestamp_ms: sequence * 500,
            latitude_deg: 45.0,
            longitude_deg: -76.0,
            speed_mps: 2.0,
            course_rad: None,
        })
    }

    #[test]
    fn arrival_during_queue_access_is_delivered() {
        let capture = Capture::default();
        capture.enable().unwrap();
        thread::scope(|scope| {
            let queue = capture.samples.lock().unwrap();
            let (started, ready) = mpsc::channel();
            let (finished, complete) = mpsc::channel();
            let capture = &capture;
            scope.spawn(move || {
                started.send(()).unwrap();
                capture.record(fix(1));
                finished.send(()).unwrap();
            });
            ready.recv().unwrap();
            let early = complete.recv_timeout(Duration::from_millis(40));
            drop(queue);
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
    fn compact_records_preserve_floats_and_use_full_headers_across_gaps() {
        let capture = Capture::default();
        capture.enable().unwrap();
        let point = |sequence, timestamp_ms| {
            Measurement::Imu(
                0,
                imu::ImuSample {
                    sequence,
                    timestamp_ms,
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
