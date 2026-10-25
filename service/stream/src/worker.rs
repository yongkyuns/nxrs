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

/// An owner-local control outcome applied before any subsequent frame I/O.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkerAction {
    Continue,
    /// Retire the active connection even when it is idle; keep the listener
    /// available for a fresh admission under the updated owner policy.
    Disconnect,
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
        now_ms: impl FnMut() -> u64,
    ) -> io::Result<()>
    where
        O: AsRef<[u8]> + AsMut<[u8]>,
        S: Read + Write,
        F: FnMut(TcpStream) -> io::Result<S>,
    {
        self.serve_with_control(server, stop, now_ms, || Ok(WorkerAction::Continue))
    }

    /// Run owner-local control before each I/O poll, including while offline,
    /// outside the frame exchange lock. The closure may apply an authenticated
    /// trust update or refresh entropy without delaying capture handoffs.
    ///
    /// Return Disconnect after an accepted policy change to retire even an idle
    /// stream before fresh admission. Continue preserves a healthy stream, for
    /// example when refusing an unauthenticated update without changing state.
    /// Control must bound its own work and native calls, for example by taking
    /// at most one command from a bounded channel with try_recv. An error closes
    /// the active connection and exits this worker; only explicit finish proves
    /// the closure's SDK/key/source owners have retired. Stop is checked again
    /// after control returns, before any new admission or frame I/O.
    pub fn serve_with_control<O, S, F>(
        &mut self,
        server: &mut Server<O, S, F>,
        stop: &WorkerStop,
        mut now_ms: impl FnMut() -> u64,
        mut control: impl FnMut() -> io::Result<WorkerAction>,
    ) -> io::Result<()>
    where
        O: AsRef<[u8]> + AsMut<[u8]>,
        S: Read + Write,
        F: FnMut(TcpStream) -> io::Result<S>,
    {
        let result = (|| {
            while !stop.is_requested() {
                if control()? == WorkerAction::Disconnect {
                    server.disconnect();
                }
                if stop.is_requested() {
                    break;
                }
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
    /// Update admission; unchanged link observations do not wake network work
    /// ahead of capture's handoff. A transition wakes the owner on its next step;
    /// this does not preempt a running native cryptographic operation.
    pub fn set_online(&self, online: bool) {
        if self.shared.online.swap(online, Ordering::AcqRel) != online {
            if let Some(thread) = &self.thread {
                thread.thread().unpark();
            }
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

#[cfg(test)]
mod tests {
    use super::*;
    use camera::{Format, PixelFormat};
    use std::sync::mpsc;

    #[test]
    fn repeated_online_observation_does_not_race_capture_handoff() {
        let (parked_send, parked) = mpsc::sync_channel(1);
        let (entered_send, entered) = mpsc::sync_channel(1);
        let (release_send, release) = mpsc::sync_channel(1);
        let mut worker = StreamWorker::spawn(
            vec![0; 64],
            WorkerOptions {
                stack_bytes: 256 * 1024,
                poll_interval: Duration::from_millis(100),
            },
            move |frames, stop| {
                while !stop.is_requested() {
                    parked_send.send(()).map_err(io::Error::other)?;
                    thread::park();
                    if stop.is_requested() {
                        break;
                    }
                    // A controlled network step holds the real exchange lock.
                    // Capture still uses the production bounded try-lock path.
                    let _slot = frames
                        .shared
                        .slot
                        .lock()
                        .map_err(|_| io::Error::other("poisoned exchange"))?;
                    entered_send.send(()).map_err(io::Error::other)?;
                    release
                        .recv_timeout(Duration::from_secs(2))
                        .map_err(io::Error::other)?;
                }
                Ok(())
            },
        )
        .unwrap();
        parked.recv_timeout(Duration::from_secs(1)).unwrap();
        worker.set_online(true);
        entered.recv_timeout(Duration::from_secs(1)).unwrap();
        release_send.send(()).unwrap();
        parked.recv_timeout(Duration::from_secs(1)).unwrap();

        // The camera composition observes its unchanged link before acquiring
        // a frame. Waking network I/O here can make the following handoff Busy.
        worker.set_online(true);
        let woke_before_capture = entered.recv_timeout(Duration::from_millis(50)).is_ok();
        let jpeg = [0xff, 0xd8, 0x11, 0x22, 0xff, 0xd9];
        let result = worker.publish(Frame {
            sequence: 1,
            capture: Capture {
                format: Format {
                    width: 320,
                    height: 240,
                    pixels: PixelFormat::Jpeg,
                },
                len: jpeg.len(),
                timestamp_ms: 0,
            },
            bytes: &jpeg,
        });
        if !woke_before_capture {
            // The successful handoff itself must still wake the consumer.
            entered.recv_timeout(Duration::from_secs(1)).unwrap();
        }
        release_send.send(()).unwrap();
        worker.finish().unwrap();
        assert_eq!(result, FramePublish::Published);
    }
}
