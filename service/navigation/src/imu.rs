use std::io;
use std::sync::mpsc::{sync_channel, SyncSender, TrySendError};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread::{self, JoinHandle};

use crate::fusion::{ImuInput, Submit};
use imu::{DeliveryError, DeviceError, ImuSample, Rejected, Session};
use messaging::{bounded, Inbox, Sender};

const INBOX_CAPACITY: usize = 32;

#[derive(Clone, Copy, Debug, Default)]
pub struct ImuProcessor {
    accel_bias: [f32; 3],
    gyro_bias: [f32; 3],
}

impl ImuProcessor {
    pub fn with_bias(accel_bias: [f32; 3], gyro_bias: [f32; 3]) -> Self {
        Self {
            accel_bias,
            gyro_bias,
        }
    }

    pub fn process(&mut self, mut sample: ImuSample) -> ImuSample {
        for index in 0..3 {
            sample.accel_mps2[index] -= self.accel_bias[index];
            sample.gyro_rps[index] -= self.gyro_bias[index];
        }
        sample
    }
}

pub struct ImuService {
    processor: ImuProcessor,
    output: ImuInput,
}
#[cfg(test)]
pub(super) struct TestInput(Sender<Event>);
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ImuStats {
    pub produced: u64,
    pub dropped: u64,
    pub errors: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ImuStatus {
    /// Processing enabled; HAL acquisition keeps draining while paused.
    pub sampling: bool,
    pub stats: ImuStats,
}
enum Event {
    Device(imu::Event),
    Pause(SyncSender<()>),
    Resume(SyncSender<()>),
    Status(SyncSender<ImuStatus>),
    Shutdown,
}
pub struct ImuHandle {
    control: Sender<Event>,
    join: Option<JoinHandle<io::Result<ImuStats>>>,
    stopping: Arc<AtomicBool>,
}
impl ImuService {
    pub fn new(output: ImuInput) -> Self {
        Self {
            processor: ImuProcessor::default(),
            output,
        }
    }
    pub fn with_processor(mut self, processor: ImuProcessor) -> Self {
        self.processor = processor;
        self
    }
    /// Acquire the caller's logical device while keeping service policy portable.
    pub fn start_on(self, selected: &'static impl imu::Device) -> io::Result<ImuHandle> {
        let (control, inbox) = bounded(INBOX_CAPACITY);
        let sender = control.clone();
        // This endpoint only admits messages; acquisition invokes it in task context.
        let sink = move |value: imu::Event| {
            sender
                .try_send(Event::Device(value))
                .map_err(|error| Rejected {
                    value,
                    error: match error {
                        TrySendError::Full(_) => DeliveryError::Full,
                        TrySendError::Disconnected(_) => DeliveryError::Closed,
                    },
                })
        };
        let device = selected.open_default(sink).map_err(hal_error)?;
        Self::start_device(self, device, control, inbox)
    }

    #[cfg(test)]
    pub(super) fn start_with_test_device<D: Session + Send + 'static>(
        self,
        device: D,
    ) -> io::Result<(ImuHandle, TestInput)> {
        let (control, inbox) = bounded(INBOX_CAPACITY);
        let inject = TestInput(control.clone());
        let handle = Self::start_device(self, device, control, inbox)?;
        Ok((handle, inject))
    }
}

#[cfg(test)]
impl TestInput {
    pub(super) fn sample(&self, sample: ImuSample) -> Result<(), ()> {
        self.0
            .try_send(Event::Device(imu::Event::Sample(sample)))
            .map_err(|_| ())
    }
}

impl ImuService {
    fn start_device<D: Session + Send + 'static>(
        self,
        device: D,
        control: Sender<Event>,
        inbox: Inbox<Event>,
    ) -> io::Result<ImuHandle> {
        let stopping = Arc::new(AtomicBool::new(false));
        let stop = stopping.clone();
        let join = thread::Builder::new()
            .name("imu-service".into())
            .spawn(move || run(self, device, inbox, &stop))?;
        Ok(ImuHandle {
            control,
            join: Some(join),
            stopping,
        })
    }
}
impl ImuHandle {
    pub fn pause(&self) -> io::Result<()> {
        self.request(Event::Pause)
    }
    pub fn resume(&self) -> io::Result<()> {
        self.request(Event::Resume)
    }
    pub fn status(&self) -> io::Result<ImuStatus> {
        let (reply, response) = sync_channel(1);
        self.send(Event::Status(reply))?;
        response
            .recv()
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "imu status reply closed"))
    }
    pub fn stop(mut self) -> io::Result<ImuStats> {
        self.request_stop();
        self.join
            .take()
            .expect("owned join")
            .join()
            .map_err(|_| io::Error::other("imu service panicked"))?
    }
    fn request_stop(&self) {
        // Shutdown has independent admission. A full inbox already wakes its owner.
        self.stopping.store(true, Ordering::Release);
        let _ = self.control.try_send(Event::Shutdown);
    }
    fn request(&self, event: impl FnOnce(SyncSender<()>) -> Event) -> io::Result<()> {
        let (reply, response) = sync_channel(1);
        self.send(event(reply))?;
        response
            .recv()
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "imu command reply closed"))
    }
    fn send(&self, event: Event) -> io::Result<()> {
        self.control
            .send(event)
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "imu service stopped"))
    }
}
fn hal_error(error: DeviceError) -> io::Error {
    let kind = match error {
        DeviceError::Unsupported => io::ErrorKind::Unsupported,
        DeviceError::Busy | DeviceError::Full => io::ErrorKind::WouldBlock,
        DeviceError::Timeout => io::ErrorKind::TimedOut,
        DeviceError::Io => io::ErrorKind::Other,
        DeviceError::InvalidData => io::ErrorKind::InvalidData,
    };
    io::Error::new(kind, format!("Imu HAL: {error:?}"))
}
impl Drop for ImuHandle {
    fn drop(&mut self) {
        self.request_stop();
    }
}
fn run<D: Session>(
    mut service: ImuService,
    mut device: D,
    inbox: Inbox<Event>,
    stopping: &AtomicBool,
) -> io::Result<ImuStats> {
    let mut stats = ImuStats::default();
    let mut sampling = true;
    let mut fault = None;
    while !stopping.load(Ordering::Acquire) {
        let Ok(event) = inbox.wait(None) else {
            break;
        };
        match event {
            Event::Pause(reply) => {
                sampling = false;
                let _ = reply.send(());
            }
            Event::Resume(reply) => {
                sampling = true;
                let _ = reply.send(());
            }
            Event::Status(reply) => {
                let mut snapshot = stats;
                snapshot.dropped = snapshot.dropped.saturating_add(u64::from(device.dropped()));
                let _ = reply.send(ImuStatus {
                    sampling,
                    stats: snapshot,
                });
            }
            Event::Device(imu::Event::Sample(value)) => {
                if !sampling {
                    stats.dropped = stats.dropped.saturating_add(1);
                    continue;
                }
                let value = service.processor.process(value);
                stats.produced = stats.produced.saturating_add(1);
                match service.output.submit(value) {
                    Submit::Accepted => {}
                    Submit::Dropped => stats.dropped = stats.dropped.saturating_add(1),
                    Submit::Disconnected => break,
                }
            }
            Event::Device(imu::Event::Failed(error)) => {
                fault = Some(error);
                break;
            }
            Event::Shutdown => break,
        }
    }
    // Teardown is outside normal event dispatch. The source never waits for queue capacity.
    let stop = device.request_stop();
    let joined = device.join();
    stats.dropped = stats.dropped.saturating_add(u64::from(device.dropped()));
    stop.and(joined).map_err(hal_error)?;
    if let Some(error) = fault {
        return Err(hal_error(error));
    }
    Ok(stats)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;
    struct Source {
        stopped: bool,
        joined: SyncSender<()>,
    }
    impl Session for Source {
        fn request_stop(&mut self) -> Result<(), DeviceError> {
            self.stopped = true;
            Ok(())
        }
        fn join(&mut self) -> Result<(), DeviceError> {
            assert!(self.stopped);
            self.joined.try_send(()).unwrap();
            Ok(())
        }
        fn is_finished(&self) -> bool {
            false
        }
        fn dropped(&self) -> u32 {
            0
        }
    }
    #[test]
    fn dropping_handle_requests_stop_even_when_measurements_fill_the_inbox() {
        let (_, inputs) = crate::FusionService::new();
        let (control, inbox) = bounded(1);
        let admitted = control.try_send(Event::Device(imu::Event::Sample(ImuSample {
            sequence: 1,
            timestamp_ms: 0,
            sample_interval_us: 10_000,
            accel_mps2: [0.0; 3],
            gyro_rps: [0.0; 3],
        })));
        assert!(admitted.is_ok());
        let stopping = Arc::new(AtomicBool::new(false));
        let stop = stopping.clone();
        let (begin, started) = sync_channel(1);
        let (joined, retired) = sync_channel(1);
        let join = thread::spawn(move || {
            started.recv().unwrap();
            run(
                ImuService::new(inputs.imu),
                Source {
                    stopped: false,
                    joined,
                },
                inbox,
                &stop,
            )
        });
        let handle = ImuHandle {
            control,
            stopping,
            join: Some(join),
        };
        drop(handle); // Shutdown cannot consume a queue slot or block this owner.
        begin.send(()).unwrap();
        retired.recv_timeout(Duration::from_secs(1)).unwrap();
    }
}
