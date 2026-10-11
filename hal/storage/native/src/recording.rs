use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::Arc;
use std::thread::{self, JoinHandle};

use storage_api::{Capture, DeviceError, Format, Frame, PixelFormat, Storage};

use crate::{device_error, invalid};

pub const RECORD_HEADER_BYTES: usize = 40;
const MAGIC: &[u8; 8] = b"RCAMREC1";

#[derive(Clone, Copy)]
pub struct StorageLimits {
    /// Maximum records waiting behind the worker. The implementation owns one
    /// additional worker payload buffer, so at most queued_records + 1 record
    /// payloads are in flight.
    pub queued_records: usize,
    pub max_frame_bytes: usize,
    /// Lifetime acceptance quota, including records already committed.
    pub max_records: u64,
}

impl StorageLimits {
    fn validate(self) -> io::Result<()> {
        if self.queued_records == 0
            || self.queued_records > 64
            || self.max_frame_bytes == 0
            || self.max_frame_bytes > 1_048_576
            || self.max_records == 0
        {
            return Err(invalid(
                "invalid storage queue, frame capacity, or record quota",
            ));
        }
        Ok(())
    }
}

#[derive(Debug)]
pub struct RecordedFrame {
    pub sequence: u64,
    pub capture: Capture,
    pub bytes: Vec<u8>,
}

struct PendingRecord {
    sequence: u64,
    capture: Capture,
    len: usize,
    bytes: Box<[u8]>,
}

impl PendingRecord {
    fn payload(&self) -> &[u8] {
        &self.bytes[..self.len]
    }
}

enum Work {
    Record(PendingRecord),
    Barrier(SyncSender<Result<(), DeviceError>>),
}

fn frame_buffer(bytes: usize) -> io::Result<Box<[u8]>> {
    let mut buffer = Vec::new();
    buffer
        .try_reserve_exact(bytes)
        .map_err(|_| io::Error::other("recording buffer allocation failed"))?;
    buffer.resize(bytes, 0);
    Ok(buffer.into_boxed_slice())
}

fn validate_frame(frame: Frame<'_>, max_bytes: usize) -> Result<(), DeviceError> {
    if frame.sequence == 0
        || frame.capture.len != frame.bytes.len()
        || !frame.capture.format.accepts_len(frame.bytes.len())
        || frame.bytes.len() > max_bytes
    {
        return Err(DeviceError::InvalidData);
    }
    Ok(())
}

fn mark_worker_failure(
    result: &mut Result<(), DeviceError>,
    failed: &AtomicBool,
) {
    if result.is_ok() {
        *result = Err(DeviceError::Io);
    }
    failed.store(true, Ordering::Release);
}

fn return_worker_slot(
    sender: &SyncSender<()>,
    result: &mut Result<(), DeviceError>,
    failed: &AtomicBool,
) {
    match sender.try_send(()) {
        Ok(()) | Err(TrySendError::Disconnected(())) => {}
        Err(TrySendError::Full(())) => mark_worker_failure(result, failed),
    }
}

fn recycle_worker_buffer(
    sender: &SyncSender<Box<[u8]>>,
    buffer: Box<[u8]>,
    result: &mut Result<(), DeviceError>,
    failed: &AtomicBool,
) {
    match sender.try_send(buffer) {
        Ok(()) | Err(TrySendError::Disconnected(_)) => {}
        Err(TrySendError::Full(_)) => mark_worker_failure(result, failed),
    }
}

/// Bounded asynchronous recording adapter.
///
/// All cross-thread payload storage is allocated during construction. The pool
/// contains queued_records + 1 buffers: at most queued_records waiting records
/// plus one record owned by the writer. append() first reserves queue capacity
/// and a free owned buffer, then copies the borrowed frame exactly once into
/// that buffer. If no queue slot is available it returns Busy before copying.
///
/// The worker returns a queue credit immediately after receiving work and
/// returns the payload buffer only after the record has completed (or has been
/// discarded because an earlier worker failure is sticky). This makes buffer
/// reuse explicit and keeps accepted payloads owned until completion.
///
/// append() acceptance is not durable storage. flush() is a nonblocking commit
/// barrier: Busy means retry; success confirms all previously accepted records
/// committed. A worker failure is sticky, reported by flush()/finish(), and
/// prevents new acceptance. Accepted records after the failing one are
/// discarded, never falsely reported as committed.
///
/// finish() explicitly closes submission, drains and joins the worker and may
/// block on filesystem I/O. Drop never joins: it closes submission and detaches
/// the worker. Callers that need completion must flush/finish explicitly. The
/// normal app flushes before dropping, so all accepted records are already
/// committed before the nonblocking detach path.
pub struct FileRecorder {
    sender: Option<SyncSender<Work>>,
    worker: Option<JoinHandle<Result<(), DeviceError>>>,
    failed: Arc<AtomicBool>,
    barrier: Option<Receiver<Result<(), DeviceError>>>,
    limits: StorageLimits,
    accepted: u64,
    free_buffers: Receiver<Box<[u8]>>,
    free_return: SyncSender<Box<[u8]>>,
    queue_slots: Receiver<()>,
    slot_return: SyncSender<()>,
}

impl FileRecorder {
    /// Creates an exclusive new session directory. Existing paths are rejected,
    /// never overwritten. Parent directory must exist. Use a trusted local
    /// filesystem supporting hard links; remote filesystem failure semantics
    /// and power-loss directory durability are not covered by this adapter.
    pub fn create_new(directory: impl AsRef<Path>, limits: StorageLimits) -> io::Result<Self> {
        limits.validate()?;
        let directory = directory.as_ref().to_path_buf();
        fs::create_dir(&directory)?;
        let writer_directory = directory.clone();
        match Self::with_writer(limits, move |record| {
            commit_record(&writer_directory, record).map_err(device_error)
        }) {
            Ok(recorder) => Ok(recorder),
            Err(error) => {
                let _ = fs::remove_dir(directory);
                Err(error)
            }
        }
    }

    fn with_writer(
        limits: StorageLimits,
        mut write: impl FnMut(&PendingRecord) -> Result<(), DeviceError> + Send + 'static,
    ) -> io::Result<Self> {
        limits.validate()?;

        let payload_count = limits
            .queued_records
            .checked_add(1)
            .ok_or_else(|| invalid("recording buffer count overflow"))?;
        let (free_return, free_buffers) = sync_channel(payload_count);
        for _ in 0..payload_count {
            free_return
                .try_send(frame_buffer(limits.max_frame_bytes)?)
                .map_err(|_| invalid("recording buffer pool initialization failed"))?;
        }

        // Credits reserve actual queue capacity before a frame payload is
        // copied. The worker returns one credit as soon as it dequeues work.
        let (slot_return, queue_slots) = sync_channel(limits.queued_records);
        for _ in 0..limits.queued_records {
            slot_return
                .try_send(())
                .map_err(|_| invalid("recording queue initialization failed"))?;
        }

        let (sender, receiver) = sync_channel(limits.queued_records);
        let worker_buffers = free_return.clone();
        let worker_slots = slot_return.clone();
        let failed = Arc::new(AtomicBool::new(false));
        let worker_failed = Arc::clone(&failed);
        let worker = thread::Builder::new()
            .name("nxrs-file-writer".into())
            .spawn(move || {
                let mut result = Ok(());
                for work in receiver {
                    return_worker_slot(&worker_slots, &mut result, &worker_failed);
                    match work {
                        Work::Record(record) => {
                            if result.is_ok() {
                                if let Err(error) = write(&record) {
                                    result = Err(error);
                                    worker_failed.store(true, Ordering::Release);
                                }
                            }
                            recycle_worker_buffer(
                                &worker_buffers,
                                record.bytes,
                                &mut result,
                                &worker_failed,
                            );
                        }
                        Work::Barrier(reply) => {
                            // One-slot reply channel and one message: this never
                            // waits for the controller to poll acknowledgement.
                            let _ = reply.send(result);
                        }
                    }
                }
                result
            })?;

        Ok(Self {
            sender: Some(sender),
            worker: Some(worker),
            failed,
            barrier: None,
            limits,
            accepted: 0,
            free_buffers,
            free_return,
            queue_slots,
            slot_return,
        })
    }

    fn reserve_queue_slot(&self) -> Result<(), DeviceError> {
        match self.queue_slots.try_recv() {
            Ok(()) => Ok(()),
            Err(TryRecvError::Empty) => Err(DeviceError::Busy),
            Err(TryRecvError::Disconnected) => Err(DeviceError::Io),
        }
    }

    fn restore_queue_slot(&self) -> Result<(), DeviceError> {
        self.slot_return.try_send(()).map_err(|_| DeviceError::Io)
    }

    fn reserve_buffer(&self) -> Result<Box<[u8]>, DeviceError> {
        match self.free_buffers.try_recv() {
            Ok(buffer) => Ok(buffer),
            // A queue credit implies one payload buffer must be available.
            // Empty therefore indicates broken internal accounting.
            Err(TryRecvError::Empty | TryRecvError::Disconnected) => Err(DeviceError::Io),
        }
    }

    fn restore_buffer(&self, buffer: Box<[u8]>) -> Result<(), DeviceError> {
        self.free_return
            .try_send(buffer)
            .map_err(|_| DeviceError::Io)
    }

    fn rollback_record(&self, record: PendingRecord) -> Result<(), DeviceError> {
        self.restore_buffer(record.bytes)?;
        self.restore_queue_slot()
    }

    fn close_submission(&mut self) {
        self.sender.take();
    }

    fn join(&mut self) -> Result<(), DeviceError> {
        self.close_submission();
        match self.worker.take() {
            Some(worker) => worker.join().unwrap_or(Err(DeviceError::Io)),
            None => Ok(()),
        }
    }

    /// Explicit blocking worker completion for non-event contexts.
    pub fn finish(mut self) -> Result<(), DeviceError> {
        self.join()
    }
}

impl Storage for FileRecorder {
    fn append(&mut self, frame: Frame<'_>) -> Result<(), DeviceError> {
        if self.failed.load(Ordering::Acquire) {
            return Err(DeviceError::Io);
        }
        if self.barrier.is_some() {
            return Err(DeviceError::Busy);
        }
        if self.accepted == self.limits.max_records {
            return Err(DeviceError::Full);
        }
        validate_frame(frame, self.limits.max_frame_bytes)?;

        // Reserve bounded queue capacity before touching the payload.
        self.reserve_queue_slot()?;
        let mut buffer = match self.reserve_buffer() {
            Ok(buffer) => buffer,
            Err(error) => {
                self.restore_queue_slot()?;
                return Err(error);
            }
        };
        let len = frame.bytes.len();
        buffer[..len].copy_from_slice(frame.bytes);
        let record = PendingRecord {
            sequence: frame.sequence,
            capture: frame.capture,
            len,
            bytes: buffer,
        };

        match self
            .sender
            .as_ref()
            .ok_or(DeviceError::Io)?
            .try_send(Work::Record(record))
        {
            Ok(()) => {
                self.accepted += 1;
                Ok(())
            }
            Err(TrySendError::Full(Work::Record(record))) => {
                self.rollback_record(record)?;
                Err(DeviceError::Busy)
            }
            Err(TrySendError::Disconnected(Work::Record(record))) => {
                self.rollback_record(record)?;
                Err(DeviceError::Io)
            }
            Err(TrySendError::Full(Work::Barrier(_))
                | TrySendError::Disconnected(Work::Barrier(_))) => unreachable!(),
        }
    }

    fn flush(&mut self) -> Result<(), DeviceError> {
        if let Some(receiver) = &self.barrier {
            return match receiver.try_recv() {
                Ok(result) => {
                    self.barrier = None;
                    result
                }
                Err(TryRecvError::Empty) => Err(DeviceError::Busy),
                Err(TryRecvError::Disconnected) => {
                    self.barrier = None;
                    Err(DeviceError::Io)
                }
            };
        }

        self.reserve_queue_slot()?;
        let (reply, receiver) = sync_channel(1);
        match self
            .sender
            .as_ref()
            .ok_or(DeviceError::Io)?
            .try_send(Work::Barrier(reply))
        {
            Ok(()) => {
                self.barrier = Some(receiver);
                Err(DeviceError::Busy)
            }
            Err(TrySendError::Full(_)) => {
                self.restore_queue_slot()?;
                Err(DeviceError::Busy)
            }
            Err(TrySendError::Disconnected(_)) => {
                self.restore_queue_slot()?;
                Err(DeviceError::Io)
            }
        }
    }
}

impl Drop for FileRecorder {
    fn drop(&mut self) {
        self.close_submission();
        // Dropping JoinHandle detaches rather than joins. This prevents an
        // unbounded filesystem wait in an event-loop Drop path.
        self.worker.take();
    }
}

fn encode_record(
    writer: &mut impl Write,
    sequence: u64,
    capture: Capture,
    bytes: &[u8],
) -> io::Result<()> {
    let mut header = [0u8; RECORD_HEADER_BYTES];
    header[..8].copy_from_slice(MAGIC);
    header[8..10].copy_from_slice(&capture.format.width.to_le_bytes());
    header[10..12].copy_from_slice(&capture.format.height.to_le_bytes());
    header[12] = match capture.format.pixels {
        PixelFormat::Gray8 => 0,
        PixelFormat::Rgb565 => 1,
        PixelFormat::Jpeg => 2,
    };
    header[16..24].copy_from_slice(&sequence.to_le_bytes());
    header[24..32].copy_from_slice(&capture.timestamp_ms.to_le_bytes());
    header[32..40].copy_from_slice(&(bytes.len() as u64).to_le_bytes());
    writer.write_all(&header)?;
    writer.write_all(bytes)
}

fn commit_record(directory: &Path, record: &PendingRecord) -> io::Result<()> {
    let pending: PathBuf = directory.join(format!("{:020}.part", record.sequence));
    let committed = directory.join(format!("{:020}.rcam", record.sequence));
    // If create_new fails, this is not our temporary file: do not delete it.
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&pending)?;
    let result = encode_record(
        &mut file,
        record.sequence,
        record.capture,
        record.payload(),
    )
    .and_then(|()| file.sync_all());
    drop(file);
    let result = result.and_then(|()| fs::hard_link(&pending, &committed));
    // Publish is the commit point. Cleanup failure must NOT turn an accepted
    // record into an error that could induce duplicate acceptance on retry.
    // A crash/cleanup failure can leave .part files; readers ignore those.
    let _ = fs::remove_file(pending);
    result
}

/// Read one committed record with a caller-supplied allocation limit.
/// The format is versioned and independent of native struct packing. Payload
/// corruption detection, crash recovery and directory fsync are not provided.
pub fn read_record(path: impl AsRef<Path>, max_frame_bytes: usize) -> io::Result<RecordedFrame> {
    let mut file = File::open(path)?;
    let mut header = [0u8; RECORD_HEADER_BYTES];
    file.read_exact(&mut header)?;
    if &header[..8] != MAGIC || header[13..16] != [0, 0, 0] {
        return Err(invalid("invalid recording header"));
    }
    let pixels = match header[12] {
        0 => PixelFormat::Gray8,
        1 => PixelFormat::Rgb565,
        2 => PixelFormat::Jpeg,
        _ => return Err(invalid("unknown recording pixel format")),
    };
    let format = Format {
        width: u16::from_le_bytes([header[8], header[9]]),
        height: u16::from_le_bytes([header[10], header[11]]),
        pixels,
    };
    let sequence = u64::from_le_bytes(header[16..24].try_into().unwrap());
    let timestamp_ms = u64::from_le_bytes(header[24..32].try_into().unwrap());
    let len = usize::try_from(u64::from_le_bytes(header[32..40].try_into().unwrap()))
        .map_err(|_| invalid("record length overflow"))?;
    if sequence == 0 || len > max_frame_bytes || !format.accepts_len(len) {
        return Err(invalid("invalid recording length or dimensions"));
    }
    let mut bytes = Vec::new();
    bytes
        .try_reserve_exact(len)
        .map_err(|_| io::Error::other("record allocation failed"))?;
    bytes.resize(len, 0);
    file.read_exact(&mut bytes)?;
    if file.read(&mut [0u8; 1])? != 0 {
        return Err(invalid("trailing recording data"));
    }
    Ok(RecordedFrame {
        sequence,
        capture: Capture {
            format,
            len,
            timestamp_ms,
        },
        bytes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    fn limits() -> StorageLimits {
        StorageLimits {
            queued_records: 1,
            max_frame_bytes: 4,
            max_records: 10,
        }
    }

    fn frame_with<'a>(sequence: u64, bytes: &'a [u8]) -> Frame<'a> {
        Frame {
            sequence,
            capture: Capture {
                format: Format {
                    width: 2,
                    height: 2,
                    pixels: PixelFormat::Gray8,
                },
                len: bytes.len(),
                timestamp_ms: sequence,
            },
            bytes,
        }
    }

    fn frame(sequence: u64) -> Frame<'static> {
        frame_with(sequence, &[7; 4])
    }

    fn flush(recorder: &mut FileRecorder) -> Result<(), DeviceError> {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            match recorder.flush() {
                Err(DeviceError::Busy) if Instant::now() < deadline => thread::yield_now(),
                result => return result,
            }
        }
    }

    fn append_retry(recorder: &mut FileRecorder, frame: Frame<'_>) {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            match recorder.append(frame) {
                Ok(()) => return,
                Err(DeviceError::Busy) if Instant::now() < deadline => thread::yield_now(),
                result => panic!("append failed: {result:?}"),
            }
        }
    }

    #[test]
    fn queue_backpressure_is_bounded_without_sleep_based_races() {
        let (entered, entering) = sync_channel(1);
        let (release, released) = sync_channel(1);
        let mut first = true;
        let mut recorder = FileRecorder::with_writer(limits(), move |_| {
            if first {
                first = false;
                entered.send(()).unwrap();
                released.recv_timeout(Duration::from_secs(10)).unwrap();
            }
            Ok(())
        })
        .unwrap();

        recorder.append(frame(1)).unwrap();
        entering.recv_timeout(Duration::from_secs(10)).unwrap();
        recorder.append(frame(2)).unwrap();
        assert_eq!(recorder.append(frame(3)), Err(DeviceError::Busy));
        assert_eq!(recorder.flush(), Err(DeviceError::Busy));
        release.send(()).unwrap();
        flush(&mut recorder).unwrap();
        assert_eq!(recorder.accepted, 2);
        recorder.finish().unwrap();
    }

    #[test]
    fn preallocated_payload_buffers_are_reused_after_completion() {
        let (seen, pointers) = sync_channel(4);
        let mut recorder = FileRecorder::with_writer(limits(), move |record| {
            seen.send((record.bytes.as_ptr() as usize, record.payload().to_vec()))
                .unwrap();
            Ok(())
        })
        .unwrap();

        append_retry(&mut recorder, frame_with(1, &[1; 4]));
        let (first_ptr, first_bytes) = pointers.recv_timeout(Duration::from_secs(10)).unwrap();
        append_retry(&mut recorder, frame_with(2, &[2; 4]));
        let (second_ptr, second_bytes) = pointers.recv_timeout(Duration::from_secs(10)).unwrap();
        append_retry(&mut recorder, frame_with(3, &[3; 4]));
        let (third_ptr, third_bytes) = pointers.recv_timeout(Duration::from_secs(10)).unwrap();

        assert_ne!(first_ptr, second_ptr);
        assert_eq!(third_ptr, first_ptr);
        assert_eq!(first_bytes, vec![1; 4]);
        assert_eq!(second_bytes, vec![2; 4]);
        assert_eq!(third_bytes, vec![3; 4]);

        recorder.finish().unwrap();
    }

    #[test]
    fn commit_failure_is_not_misreported_as_durable_acceptance() {
        let mut recorder = FileRecorder::with_writer(limits(), |_| Err(DeviceError::Io)).unwrap();
        recorder.append(frame(1)).unwrap();
        assert_eq!(flush(&mut recorder), Err(DeviceError::Io));
        assert_eq!(recorder.append(frame(2)), Err(DeviceError::Io));
        assert_eq!(recorder.finish(), Err(DeviceError::Io));
    }

    #[test]
    fn barrier_blocks_new_acceptance_until_acknowledged() {
        let mut recorder = FileRecorder::with_writer(limits(), |_| Ok(())).unwrap();
        assert_eq!(recorder.flush(), Err(DeviceError::Busy));
        assert_eq!(recorder.append(frame(1)), Err(DeviceError::Busy));
        flush(&mut recorder).unwrap();
        recorder.append(frame(1)).unwrap();
        recorder.finish().unwrap();
    }

    #[test]
    fn drop_closes_submission_without_joining_a_blocked_worker() {
        let (entered, entering) = sync_channel(1);
        let (release, released) = sync_channel(1);
        let (exited, exit_seen) = sync_channel(1);
        let mut recorder = FileRecorder::with_writer(limits(), move |_| {
            entered.send(()).unwrap();
            released.recv_timeout(Duration::from_secs(10)).unwrap();
            exited.send(()).unwrap();
            Ok(())
        })
        .unwrap();
        recorder.append(frame(1)).unwrap();
        entering.recv_timeout(Duration::from_secs(10)).unwrap();

        let (dropped, drop_seen) = sync_channel(1);
        let dropper = thread::spawn(move || {
            drop(recorder);
            dropped.send(()).unwrap();
        });
        drop_seen
            .recv_timeout(Duration::from_secs(1))
            .expect("Drop unexpectedly joined the blocked writer");
        release.send(()).unwrap();
        exit_seen.recv_timeout(Duration::from_secs(10)).unwrap();
        dropper.join().unwrap();
    }

    #[test]
    fn explicit_finish_waits_for_worker_completion() {
        let (entered, entering) = sync_channel(1);
        let (release, released) = sync_channel(1);
        let mut recorder = FileRecorder::with_writer(limits(), move |_| {
            entered.send(()).unwrap();
            released.recv_timeout(Duration::from_secs(10)).unwrap();
            Ok(())
        })
        .unwrap();
        recorder.append(frame(1)).unwrap();
        entering.recv_timeout(Duration::from_secs(10)).unwrap();

        let (finished, finish_result) = sync_channel(1);
        let finisher = thread::spawn(move || {
            finished.send(recorder.finish()).unwrap();
        });
        assert_eq!(finish_result.try_recv(), Err(TryRecvError::Empty));
        release.send(()).unwrap();
        assert_eq!(
            finish_result.recv_timeout(Duration::from_secs(10)).unwrap(),
            Ok(())
        );
        finisher.join().unwrap();
    }

    #[test]
    fn encoder_propagates_a_partial_write_failure() {
        struct FailingWriter(usize);
        impl Write for FailingWriter {
            fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
                if self.0 == 0 {
                    return Err(io::Error::other("injected write failure"));
                }
                let n = bytes.len().min(self.0);
                self.0 -= n;
                Ok(n)
            }

            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }

        let capture = frame(1).capture;
        assert!(
            encode_record(
                &mut FailingWriter(RECORD_HEADER_BYTES + 2),
                1,
                capture,
                &[7; 4],
            )
            .is_err()
        );
    }
}
