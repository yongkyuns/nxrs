//! One network owner with a bounded latest-frame handoff from camera capture.
use crate::{reject_frame, RejectReason, Server, ServerStats};
use camera::{Capture, Frame};
use std::io::{self, Read, Write};
use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, TryLockError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

#[derive(Clone, Copy, Debug)]
pub struct WorkerOptions {
    /// Platform-supplied native stack budget; zero is refused.
    pub stack_bytes: usize,
    /// Maximum idle park between polls, positive and at most 100 ms.
    pub poll_interval: Duration,
}
impl WorkerOptions {
    fn validate(self) -> io::Result<Self> {
        if self.stack_bytes == 0
            || self.poll_interval.is_zero()
            || self.poll_interval > Duration::from_millis(100)
        {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        Ok(self)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FramePublish {
    Published,
    /// The consumer currently owns the buffer; no capture-side wait or copy.
    Busy,
    Rejected(RejectReason),
    Closed,
}

#[derive(Clone)]
pub struct WorkerStop(Arc<AtomicBool>);
impl WorkerStop {
    pub fn is_requested(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}
struct Slot<B> {
    bytes: B,
    metadata: Option<(u64, Capture)>,
    stats: ServerStats,
}
struct Shared<B> {
    slot: Mutex<Slot<B>>,
    receiver_closed: AtomicBool,
    online: AtomicBool,
}

/// Unique consumer on the network worker. A frame borrow never escapes its
/// synchronous poll; the producer uses try_lock and drops overload instead of
/// waiting for native cryptography or network I/O.
pub struct WorkerReceiver<B> {
    shared: Arc<Shared<B>>,
    options: WorkerOptions,
}
impl<B: AsRef<[u8]> + AsMut<[u8]>> WorkerReceiver<B> {
    /// Drive the actual HTTP/MJPEG owner with the same monotonic clock used for
    /// capture timestamps. The caller constructs thread-confined credentials and
    /// server inside the worker closure before calling this method.
    ///
    /// Stop is cooperative. Native calls must honor their own finite bounds.
    /// Every ordinary error and stop path disconnects the active server before
    /// returning to the closure that owns its key and trust state.
    pub fn serve<O, S, F>(
        &mut self,
        server: &mut Server<O, S, F>,
        stop: &WorkerStop,
        mut now_ms: impl FnMut() -> u64,
    ) -> io::Result<()>
    where
        O: AsRef<[u8]> + AsMut<[u8]>,
        S: Read + Write,
        F: FnMut(TcpStream) -> io::Result<S>,
    {
        let result = (|| {
            while !stop.is_requested() {
                if !self.shared.online.load(Ordering::Acquire) {
                    server.disconnect();
                } else {
                    let now = now_ms();
                    match self.shared.slot.try_lock() {
                        Ok(slot) => {
                            let latest = slot.metadata.map(|(sequence, capture)| Frame {
                                sequence,
                                capture,
                                bytes: &slot.bytes.as_ref()[..capture.len],
                            });
                            server.poll(now, latest)?;
                        }
                        Err(TryLockError::WouldBlock) => {
                            // Capture may be copying its latest bounded frame.
                            // Keep handshake/deadline progress independent of it.
                            server.poll(now, None)?;
                        }
                        Err(TryLockError::Poisoned(_)) => {
                            return Err(io::Error::other("frame exchange poisoned"))
                        }
                    }
                }
                if let Ok(mut slot) = self.shared.slot.try_lock() {
                    slot.stats = server.stats();
                }
                thread::park_timeout(self.options.poll_interval);
            }
            Ok(())
        })();
        server.disconnect();
        if let Ok(mut slot) = self.shared.slot.try_lock() {
            slot.stats = server.stats();
        }
        result
    }
}
impl<B> Drop for WorkerReceiver<B> {
    fn drop(&mut self) {
        self.shared.receiver_closed.store(true, Ordering::Release);
    }
}

/// A single network worker plus one fixed-capacity shared latest frame. The run
/// closure constructs all !Send credentials/SDK state on the worker itself.
/// Capture-side publish/stats/online/stop operations never join or wait on I/O.
/// Explicit finish joins after owned native state has retired. Drop requests
/// stop and detaches; the worker retains its resources until it actually exits.
#[must_use = "keep the worker handle and explicitly finish it to observe retirement"]
pub struct StreamWorker<B> {
    shared: Arc<Shared<B>>,
    stop: WorkerStop,
    thread: Option<JoinHandle<io::Result<()>>>,
}
impl<B: AsRef<[u8]> + AsMut<[u8]> + Send + 'static> StreamWorker<B> {
    /// Start offline. The caller enables admission only after capture/link policy
    /// is ready. The exchange and server outgoing buffer are distinct resources.
    pub fn spawn(
        mut buffer: B,
        options: WorkerOptions,
        run: impl FnOnce(WorkerReceiver<B>, WorkerStop) -> io::Result<()> + Send + 'static,
    ) -> io::Result<Self> {
        let options = options.validate()?;
        if buffer.as_ref().is_empty() || buffer.as_ref().len() != buffer.as_mut().len() {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        let shared = Arc::new(Shared {
            slot: Mutex::new(Slot {
                bytes: buffer,
                metadata: None,
                stats: ServerStats::default(),
            }),
            receiver_closed: AtomicBool::new(false),
            online: AtomicBool::new(false),
        });
        let stop = WorkerStop(Arc::new(AtomicBool::new(false)));
        let receiver = WorkerReceiver {
            shared: shared.clone(),
            options,
        };
        let worker_stop = stop.clone();
        let thread = thread::Builder::new()
            .name("nxrs-stream".into())
            .stack_size(options.stack_bytes)
            .spawn(move || run(receiver, worker_stop))?;
        Ok(Self {
            shared,
            stop,
            thread: Some(thread),
        })
    }
    pub fn publish(&mut self, frame: Frame<'_>) -> FramePublish {
        if self.stop.is_requested() || self.shared.receiver_closed.load(Ordering::Acquire) {
            return FramePublish::Closed;
        }
        let mut slot = match self.shared.slot.try_lock() {
            Ok(slot) => slot,
            Err(TryLockError::WouldBlock) => return FramePublish::Busy,
            Err(TryLockError::Poisoned(_)) => return FramePublish::Closed,
        };
        if let Some(reason) = reject_frame(&frame, slot.bytes.as_ref().len()) {
            return FramePublish::Rejected(reason);
        }
        slot.bytes.as_mut()[..frame.bytes.len()].copy_from_slice(frame.bytes);
        slot.metadata = Some((frame.sequence, frame.capture));
        drop(slot);
        if let Some(thread) = &self.thread {
            thread.thread().unpark();
        }
        FramePublish::Published
    }
    /// Disable new admission and retire the active connection on the next worker
    /// step. This does not preempt a running native cryptographic operation.
    pub fn set_online(&self, online: bool) {
        self.shared.online.store(online, Ordering::Release);
        if let Some(thread) = &self.thread {
            thread.thread().unpark();
        }
    }
    /// A busy worker yields None; capture does not wait for its frame lock.
    pub fn stats(&self) -> Option<ServerStats> {
        self.shared.slot.try_lock().ok().map(|slot| slot.stats)
    }
    pub fn is_finished(&self) -> bool {
        self.thread.as_ref().is_none_or(JoinHandle::is_finished)
    }
    pub fn request_stop(&self) {
        self.stop.0.store(true, Ordering::Release);
        if let Some(thread) = &self.thread {
            thread.thread().unpark();
        }
    }
    /// Explicit barrier. May wait for the provider's current native operation;
    /// call it during shutdown, outside the camera capture polling loop.
    pub fn finish(mut self) -> io::Result<ServerStats> {
        self.request_stop();
        self.thread
            .take()
            .expect("live worker handle")
            .join()
            .map_err(|_| io::Error::other("network worker panicked"))??;
        self.stats()
            .ok_or_else(|| io::Error::other("frame exchange poisoned"))
    }
}
impl<B> Drop for StreamWorker<B> {
    fn drop(&mut self) {
        self.stop.0.store(true, Ordering::Release);
        if let Some(thread) = &self.thread {
            thread.thread().unpark();
        }
    }
}
