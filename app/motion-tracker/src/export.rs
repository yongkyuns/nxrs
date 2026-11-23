//! Bounded capture for the authenticated tracker export stream.
use navigation::observations::Measurement;
use std::collections::VecDeque;
use std::io;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex,
};

pub const REQUEST: &[u8; 5] = b"NXRS\x01";
const QUEUE: usize = 128;
const BATCH: usize = 16;
pub const MAX_BYTES: usize = 4 + BATCH * 33;
pub const OVERFLOW_FRAME: [u8; 4] = [0xa5, 1, 0, 0];

#[derive(Default)]
pub struct Capture {
    enabled: AtomicBool,
    samples: Mutex<Samples>,
}

#[derive(Default)]
struct Samples {
    queue: VecDeque<Measurement>,
    overflowed: bool,
}

impl Capture {
    pub fn enable(&self) -> io::Result<()> {
        let mut samples = self.samples.lock().unwrap();
        samples.queue.clear();
        samples
            .queue
            .try_reserve_exact(QUEUE)
            .map_err(io::Error::other)?;
        samples.overflowed = false;
        self.enabled.store(true, Ordering::Release);
        Ok(())
    }

    pub fn disable(&self) {
        self.enabled.store(false, Ordering::Release);
    }

    pub fn record(&self, sample: Measurement) {
        if !self.enabled.load(Ordering::Acquire) {
            return;
        }
        // This runs in the acquisition task. The consumer holds the lock
        // only for a bounded copy, with no encoding or transport I/O.
        let mut samples = self.samples.lock().unwrap();
        if !self.enabled.load(Ordering::Relaxed) {
            return;
        }
        if samples.queue.len() == QUEUE {
            samples.overflowed = true;
            self.disable();
            return;
        }
        samples.queue.push_back(sample);
    }

    pub fn take(&self, output: &mut [u8; MAX_BYTES]) -> usize {
        let mut batch = [None; BATCH];
        let count = {
            let mut samples = self.samples.lock().unwrap();
            let count = samples.queue.len().min(BATCH);
            if count == 0 {
                if samples.overflowed {
                    output[..OVERFLOW_FRAME.len()].copy_from_slice(&OVERFLOW_FRAME);
                    return OVERFLOW_FRAME.len();
                }
                return 0;
            }
            for sample in &mut batch[..count] {
                *sample = samples.queue.pop_front();
            }
            count
        };
        let mut end = 4;
        for sample in batch.into_iter().take(count).flatten() {
            end += encode(sample, &mut output[end..]);
        }
        output[..2].copy_from_slice(&[0xa5, 1]);
        output[2..4].copy_from_slice(&((end - 4) as u16).to_le_bytes());
        end
    }
}

fn encode(sample: Measurement, output: &mut [u8]) -> usize {
    let (tag, sequence, time) = match sample {
        Measurement::Imu(index, sample) => (1 + index as u8, sample.sequence, sample.timestamp_ms),
        Measurement::Gnss(fix) => (3, fix.sequence, fix.timestamp_ms),
    };
    output[0] = tag;
    output[1..5].copy_from_slice(&(sequence as u32).to_le_bytes());
    output[5..9].copy_from_slice(&(time as u32).to_le_bytes());
    match sample {
        Measurement::Imu(_, sample) => {
            for (field, value) in output[9..33]
                .chunks_exact_mut(4)
                .zip(sample.accel_mps2.into_iter().chain(sample.gyro_rps))
            {
                field.copy_from_slice(&value.to_le_bytes());
            }
            33
        }
        Measurement::Gnss(fix) => {
            output[9..17].copy_from_slice(&fix.latitude_deg.to_le_bytes());
            output[17..25].copy_from_slice(&fix.longitude_deg.to_le_bytes());
            output[25..29].copy_from_slice(&fix.speed_mps.to_le_bytes());
            29
        }
    }
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
        assert_eq!(capture.take(&mut bytes), 33);
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
            assert_eq!(capture.take(&mut bytes), 4 + BATCH * 29);
            assert_eq!(&bytes[5..9], &(first as u32).to_le_bytes());
        }
        assert_eq!(capture.take(&mut bytes), OVERFLOW_FRAME.len());
        assert_eq!(&bytes[..OVERFLOW_FRAME.len()], &OVERFLOW_FRAME);
        capture.enable().unwrap();
        assert_eq!(capture.take(&mut bytes), 0);
        capture.record(fix(QUEUE as u64 + 2));
        assert_eq!(capture.take(&mut bytes), 33);
    }
}
