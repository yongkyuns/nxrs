use crate::{Error, Observation, PersonDetector};
use camera::{Frame, PixelFormat};
use std::sync::{Arc, Condvar, Mutex, TryLockError};
use std::thread::{Builder, JoinHandle};
use std::time::Instant;

#[derive(Clone, Copy, Debug)]
pub struct WorkerOptions {
    pub frame_capacity: usize,
    pub sample_interval_ms: u64,
    pub stack_bytes: usize,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Offer {
    Accepted,
    Busy,
    NotDue,
}
#[derive(Clone, Copy, Debug, Default)]
pub struct WorkerStats {
    pub submitted: u64,
    pub completed: u64,
    pub skipped_busy: u64,
    pub failures: u64,
    pub inference_us: u64,
    pub max_inference_us: u64,
}
struct Job {
    bytes: Vec<u8>,
    sequence: u64,
    captured_ms: u64,
    width: u16,
    height: u16,
}
struct Completed {
    job: Job,
    result: Result<Observation, Error>,
}
#[derive(Default)]
struct Mailbox {
    job: Option<Job>,
    completed: Option<Completed>,
    ready: Option<Result<(), Error>>,
    closed: bool,
}
#[derive(Default)]
struct Shared {
    mailbox: Mutex<Mailbox>,
    changed: Condvar,
}
// Publish a startup failure if the initializer unwinds on an unwind-capable
// host. Target aborts still terminate the process and cannot be recovered here.
struct Starting {
    shared: Arc<Shared>,
    armed: bool,
}
impl Drop for Starting {
    fn drop(&mut self) {
        if self.armed {
            let mut state = self
                .shared
                .mailbox
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            state.ready = Some(Err(Error::Worker));
            state.closed = true;
            self.shared.changed.notify_all();
        }
    }
}

/// Exactly one owned JPEG moves through a single mailbox. The producer only
/// tries its lock: a preempted lower-priority worker cannot make capture spin.
/// Model execution, JPEG copying and allocation never hold the mailbox lock.
pub struct Worker {
    shared: Arc<Shared>,
    thread: Option<JoinHandle<()>>,
    free: Option<Vec<u8>>,
    options: WorkerOptions,
    last_sequence: u64,
    next_ms: u64,
    last_now_ms: u64,
    failed: bool,
    stopped: bool,
    stats: WorkerStats,
}
impl Worker {
    /// Product wiring supplies resources and execution-context initialization.
    /// Initialization is acknowledged before spawn returns; capture has not
    /// started yet, so waiting here does not block the acquisition loop.
    pub fn spawn<D: PersonDetector + 'static>(
        options: WorkerOptions,
        initialize: impl FnOnce() -> Result<D, Error> + Send + 'static,
    ) -> Result<Self, Error> {
        if options.frame_capacity == 0
            || options.frame_capacity > 153600
            || options.sample_interval_ms == 0
            || options.stack_bytes < 16384
        {
            return Err(Error::Configuration);
        }
        let mut bytes = Vec::new();
        bytes
            .try_reserve_exact(options.frame_capacity)
            .map_err(|_| Error::Configuration)?;
        let shared = Arc::new(Shared::default());
        let worker = shared.clone();
        let thread = Builder::new()
            .name("person-detection".into())
            .stack_size(options.stack_bytes)
            .spawn(move || {
                let mut starting = Starting {
                    shared: worker.clone(),
                    armed: true,
                };
                let initialized = initialize();
                let mut state = worker.mailbox.lock().unwrap_or_else(|e| e.into_inner());
                state.ready = Some(initialized.as_ref().map(|_| ()).map_err(|&e| e));
                starting.armed = false;
                worker.changed.notify_all();
                let Ok(mut detector) = initialized else {
                    return;
                };
                loop {
                    while state.job.is_none() && !state.closed {
                        let Ok(next) = worker.changed.wait(state) else {
                            return;
                        };
                        state = next;
                    }
                    let Some(job) = state.job.take() else {
                        drop(state);
                        return;
                    };
                    drop(state);
                    let start = Instant::now();
                    let result = detector
                        .detect(detector::Image::jpeg(&job.bytes, job.width, job.height))
                        .map_err(Error::from)
                        .map(|result| Observation {
                            sequence: job.sequence,
                            captured_ms: job.captured_ms,
                            inference_us: start.elapsed().as_micros().min(u64::MAX as u128) as u64,
                            score: result.person_score,
                            objects: result.objects,
                        });
                    state = match worker.mailbox.lock() {
                        Ok(state) => state,
                        Err(_) => return,
                    };
                    // Admission owns the only JPEG, so no second job/result can
                    // exist. Publish the entire completed value under one lock.
                    state.completed = Some(Completed { job, result });
                }
            })
            .map_err(|_| Error::Worker)?;
        let initialized = {
            let mut state = shared.mailbox.lock().unwrap_or_else(|e| e.into_inner());
            while state.ready.is_none() {
                state = shared
                    .changed
                    .wait(state)
                    .unwrap_or_else(|e| e.into_inner());
            }
            state.ready.take().unwrap()
        };
        if let Err(error) = initialized {
            let _ = thread.join();
            return Err(error);
        }
        Ok(Self {
            shared,
            thread: Some(thread),
            free: Some(bytes),
            options,
            last_sequence: 0,
            next_ms: 0,
            last_now_ms: 0,
            failed: false,
            stopped: false,
            stats: WorkerStats::default(),
        })
    }
    pub fn stats(&self) -> WorkerStats {
        self.stats
    }
    pub fn offer(&mut self, frame: Frame<'_>, now_ms: u64) -> Result<Offer, Error> {
        if self.failed {
            return Err(Error::Worker);
        }
        if self.stopped {
            return Err(Error::Stopped);
        }
        if now_ms < self.last_now_ms {
            return Err(Error::Clock);
        }
        self.last_now_ms = now_ms;
        if frame.sequence <= self.last_sequence || now_ms < self.next_ms {
            return Ok(Offer::NotDue);
        }
        if frame.capture.format.pixels != PixelFormat::Jpeg
            || frame.capture.len != frame.bytes.len()
            || frame.bytes.is_empty()
            || frame.capture.format.width == 0
            || frame.capture.format.height == 0
            || frame.bytes.len() > self.options.frame_capacity
            || frame.capture.timestamp_ms > now_ms
        {
            return Err(Error::Frame);
        }
        let Some(mut bytes) = self.free.take() else {
            self.stats.skipped_busy = self.stats.skipped_busy.saturating_add(1);
            return Ok(Offer::Busy);
        };
        bytes.clear();
        bytes.extend_from_slice(frame.bytes);
        let mut state = match self.shared.mailbox.try_lock() {
            Ok(state) => state,
            Err(TryLockError::WouldBlock) => {
                self.free = Some(bytes);
                self.stats.skipped_busy = self.stats.skipped_busy.saturating_add(1);
                return Ok(Offer::Busy);
            }
            Err(TryLockError::Poisoned(_)) => {
                self.free = Some(bytes);
                self.failed = true;
                return Err(Error::Worker);
            }
        };
        if state.closed || state.job.is_some() || state.completed.is_some() {
            self.free = Some(bytes);
            self.failed = true;
            return Err(Error::Worker);
        }
        state.job = Some(Job {
            bytes,
            sequence: frame.sequence,
            captured_ms: frame.capture.timestamp_ms,
            width: frame.capture.format.width,
            height: frame.capture.format.height,
        });
        drop(state);
        self.shared.changed.notify_one();
        self.stats.submitted = self.stats.submitted.saturating_add(1);
        self.last_sequence = frame.sequence;
        self.next_ms = now_ms.saturating_add(self.options.sample_interval_ms);
        Ok(Offer::Accepted)
    }
    pub fn poll(&mut self) -> Result<Option<Observation>, Error> {
        if self.stopped {
            return Err(Error::Stopped);
        }
        if self.failed {
            return Err(Error::Worker);
        }
        let completed = match self.shared.mailbox.try_lock() {
            Ok(mut state) => state.completed.take(),
            Err(TryLockError::WouldBlock) => return Ok(None),
            Err(TryLockError::Poisoned(_)) => {
                self.failed = true;
                return Err(Error::Worker);
            }
        };
        if let Some(done) = completed {
            self.free = Some(done.job.bytes);
            match done.result {
                Ok(observation) => {
                    self.stats.completed = self.stats.completed.saturating_add(1);
                    self.stats.inference_us = self
                        .stats
                        .inference_us
                        .saturating_add(observation.inference_us);
                    self.stats.max_inference_us =
                        self.stats.max_inference_us.max(observation.inference_us);
                    Ok(Some(observation))
                }
                Err(error) => {
                    self.stats.failures = self.stats.failures.saturating_add(1);
                    Err(error)
                }
            }
        } else if self.thread.as_ref().is_some_and(JoinHandle::is_finished) {
            self.failed = true;
            Err(Error::Worker)
        } else {
            Ok(None)
        }
    }
    /// Stop can wait: close admission, wake the worker, finish its one admitted
    /// job, and join before releasing any model or frame storage.
    pub fn stop(&mut self) -> Result<(), Error> {
        self.stopped = true;
        {
            let mut state = self.shared.mailbox.lock().unwrap_or_else(|e| {
                self.failed = true;
                e.into_inner()
            });
            state.closed = true;
        }
        self.shared.changed.notify_all();
        if let Some(thread) = self.thread.take() {
            if thread.join().is_err() {
                self.failed = true;
            }
        }
        if self.failed {
            Err(Error::Worker)
        } else {
            Ok(())
        }
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Immediate;
    impl PersonDetector for Immediate {
        fn capabilities(&self) -> detector::Capabilities {
            detector::Capabilities::presence(96, 96)
        }
        fn detect(
            &mut self,
            _: detector::Image<'_>,
        ) -> Result<detector::DetectionResult, detector::DeviceError> {
            Ok(detector::DetectionResult::presence(crate::Score::new(900)?))
        }
    }
    fn options() -> WorkerOptions {
        WorkerOptions {
            frame_capacity: 4,
            sample_interval_ms: 1,
            stack_bytes: 65536,
        }
    }
    #[test]
    fn preempted_mailbox_owner_cannot_block_poll_or_admission() {
        let mut worker = Worker::spawn(options(), || Ok(Immediate)).unwrap();
        let shared = worker.shared.clone();
        let held = shared.mailbox.lock().unwrap();
        assert_eq!(worker.poll(), Ok(None));
        let frame = Frame {
            sequence: 1,
            capture: camera::Capture {
                format: camera::Format {
                    width: 1,
                    height: 1,
                    pixels: PixelFormat::Jpeg,
                },
                len: 4,
                timestamp_ms: 1,
            },
            bytes: &[255, 216, 255, 217],
        };
        assert_eq!(worker.offer(frame, 1), Ok(Offer::Busy));
        assert_eq!(worker.stats().submitted, 0);
        drop(held);
        assert_eq!(worker.offer(frame, 1), Ok(Offer::Accepted));
        worker.stop().unwrap();
    }
    #[test]
    fn initializer_unwind_acknowledges_failure_instead_of_stranding_startup() {
        let result = Worker::spawn::<Immediate>(options(), || panic!("initializer witness"));
        assert!(matches!(result, Err(Error::Worker)));
    }
}
