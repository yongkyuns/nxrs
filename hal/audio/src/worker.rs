//! Signal-driven DMA draining, independent of a slow or preempted consumer.
use crate::{Block, Capture, DeviceError, Stream, SAMPLES_PER_BLOCK};
use acquisition::{Wake, Worker};
use messaging::{DeliveryError, Rejected};
use std::{
    sync::{atomic::Ordering, Arc, Mutex, TryLockError},
    time::{Duration, Instant},
};

const READ_TIMEOUT: Duration = Duration::from_millis(100);
const STOP_TIMEOUT: Duration = Duration::from_millis(250);

pub(crate) struct Buffered<C: Wake> {
    block: Arc<Mutex<Option<Block>>>,
    worker: Worker<C, DeviceError>,
}

impl<C: Wake> Buffered<C> {
    pub(crate) fn start<T: Capture>(
        cancel: C,
        stack_bytes: usize,
        open: impl FnOnce() -> Result<T, DeviceError> + Send + 'static,
    ) -> Result<Self, DeviceError> {
        let block = Arc::new(Mutex::new(None));
        let mailbox = block.clone();
        let worker = Worker::start(
            "microphone",
            stack_bytes,
            cancel,
            move |event: Result<Block, DeviceError>| {
                let Ok(block) = event else {
                    // The joined result is independent of the sample slot.
                    return Ok(());
                };
                match mailbox.try_lock() {
                    Ok(mut latest) => {
                        *latest = Some(block);
                        Ok(())
                    }
                    Err(TryLockError::WouldBlock) => Err(Rejected {
                        value: Ok(block),
                        error: DeliveryError::Full,
                    }),
                    Err(TryLockError::Poisoned(_)) => Err(Rejected {
                        value: Ok(block),
                        error: DeliveryError::Closed,
                    }),
                }
            },
            Err,
            move |stop, emit| {
                if stop.load(Ordering::Acquire) {
                    return Ok(());
                }
                let mut input = open()?;
                let result = loop {
                    if stop.load(Ordering::Acquire) {
                        break Ok(());
                    }
                    let mut samples = [0; SAMPLES_PER_BLOCK];
                    let read = input.read(Instant::now() + READ_TIMEOUT, &mut samples);
                    if stop.load(Ordering::Acquire) {
                        break Ok(());
                    }
                    match read {
                        Ok(frame) if emit(Ok(Block { frame, samples })) => {}
                        Ok(_) => break Err(DeviceError::Io),
                        Err(error) => break Err(error),
                    }
                };
                input.stop(Instant::now() + STOP_TIMEOUT).and(result)
            },
        )
        .map_err(|_| DeviceError::Io)?;
        Ok(Self { block, worker })
    }
}

impl<C: Wake> Stream for Buffered<C> {
    fn take(&mut self) -> Result<Option<Block>, DeviceError> {
        if self.worker.is_finished() {
            self.worker.join(DeviceError::Io)?;
            return Err(DeviceError::Closed);
        }
        match self.block.try_lock() {
            Ok(mut block) => Ok(block.take()),
            Err(TryLockError::WouldBlock) => Ok(None),
            Err(TryLockError::Poisoned(_)) => Err(DeviceError::Io),
        }
    }

    fn stop(&mut self) -> Result<(), DeviceError> {
        // A wake error still leaves an absolute deadline on the native wait.
        // Join even in that case so no audio can outlive this session.
        if !self.worker.is_finished() {
            let _ = self.worker.request_stop();
        }
        self.worker.join(DeviceError::Io)
    }
}

impl<C: Wake> Drop for Buffered<C> {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Frame;
    use std::io;
    use std::sync::mpsc::{self, Receiver, Sender};

    struct Cancel(Sender<Result<u64, DeviceError>>);
    impl Wake for Cancel {
        fn wake(&self) -> io::Result<()> {
            self.0
                .send(Err(DeviceError::Closed))
                .map_err(io::Error::other)
        }
    }

    struct Input {
        next: Receiver<Result<u64, DeviceError>>,
        waiting: Sender<()>,
        stopped: Sender<()>,
    }
    impl Capture for Input {
        fn read(
            &mut self,
            until: Instant,
            samples: &mut [i16; SAMPLES_PER_BLOCK],
        ) -> Result<Frame, DeviceError> {
            self.waiting.send(()).map_err(|_| DeviceError::Io)?;
            let first_sample = self
                .next
                .recv_timeout(until.saturating_duration_since(Instant::now()))
                .map_err(|_| DeviceError::Timeout)??;
            samples.fill(-321);
            Ok(Frame {
                first_sample,
                observed_at: Instant::now(),
            })
        }

        fn stop(&mut self, _: Instant) -> Result<(), DeviceError> {
            self.stopped.send(()).map_err(|_| DeviceError::Io)
        }
    }

    #[test]
    fn retains_only_the_latest_block_and_joins_after_waking_capture() {
        let (tx, rx) = mpsc::channel();
        let (waiting, reading) = mpsc::channel();
        let (stopped, done) = mpsc::channel();
        let mut stream = Buffered::start(Cancel(tx.clone()), 64 * 1024, move || {
            Ok(Input {
                next: rx,
                waiting,
                stopped,
            })
        })
        .unwrap();
        reading.recv_timeout(Duration::from_secs(1)).unwrap();
        for sample in [0, SAMPLES_PER_BLOCK as u64] {
            tx.send(Ok(sample)).unwrap();
            reading.recv_timeout(Duration::from_secs(1)).unwrap();
        }
        let block = stream.take().unwrap().unwrap();
        assert_eq!(block.frame.first_sample, SAMPLES_PER_BLOCK as u64);
        assert_eq!(block.samples, [-321; SAMPLES_PER_BLOCK]);
        assert!(stream.take().unwrap().is_none());
        assert!(done.try_recv().is_err());
        assert_eq!(stream.stop(), Ok(()));
        assert_eq!(done.try_recv(), Ok(()));
        assert!(matches!(stream.take(), Err(DeviceError::Closed)));
    }
}
