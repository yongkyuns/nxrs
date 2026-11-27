use std::f32::consts::PI;
use std::io;
use std::sync::mpsc::{sync_channel, SyncSender};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread::{self, JoinHandle};

use crate::fusion::{GnssInput, Submit};
use crate::{control, ControlCompletion, ControlRequest, RequestError, RequestId};
use gnss::{DeviceError, GnssFix, Session};
use messaging::{bounded, Inbox, Sender};

const INBOX_CAPACITY: usize = 32;

#[derive(Clone, Copy, Debug)]
pub struct GnssProcessor {
    course_offset_rad: f32,
}

impl Default for GnssProcessor {
    fn default() -> Self {
        Self {
            course_offset_rad: 0.0,
        }
    }
}

impl GnssProcessor {
    pub fn with_course_offset(course_offset_rad: f32) -> Self {
        Self { course_offset_rad }
    }

    pub fn process(&mut self, mut fix: GnssFix) -> GnssFix {
        fix.speed_mps = fix.speed_mps.max(0.0);
        fix.course_rad = fix
            .course_rad
            .map(|course| wrap_pi(course + self.course_offset_rad));
        fix
    }
}

fn wrap_pi(mut value: f32) -> f32 {
    while value > PI {
        value -= 2.0 * PI;
    }
    while value < -PI {
        value += 2.0 * PI;
    }
    value
}

pub struct GnssService {
    processor: GnssProcessor,
    output: GnssInput,
}
#[cfg(test)]
pub(super) struct TestInput(Sender<Event>);
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct GnssStats {
    pub produced: u64,
    pub dropped: u64,
    pub errors: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct GnssStatus {
    /// Processing enabled; HAL acquisition keeps draining while paused.
    pub sampling: bool,
    pub stats: GnssStats,
}
enum Event {
    Device(gnss::Event),
    Control(control::Ticket),
    Pause(SyncSender<()>),
    Resume(SyncSender<()>),
    Status(SyncSender<GnssStatus>),
    Shutdown,
}
pub struct GnssHandle {
    control: Sender<Event>,
    replies: control::Client<GnssStatus>,
    join: Option<JoinHandle<io::Result<GnssStats>>>,
    stopping: Arc<AtomicBool>,
}
impl GnssService {
    pub fn new(output: GnssInput) -> Self {
        Self {
            processor: GnssProcessor::default(),
            output,
        }
    }
    pub fn with_processor(mut self, processor: GnssProcessor) -> Self {
        self.processor = processor;
        self
    }
    /// Start with a retained completion slot but no notification callback.
    /// Blocking compatibility requests are separate from that retained slot.
    pub fn start_on(self, device: &'static impl gnss::Device) -> io::Result<GnssHandle> {
        self.start_on_with_wake(device, || {})
    }

    /// Start with one retained completion slot and the caller's existing wake path.
    ///
    /// `wake` normally runs on the service worker after publishing a completion
    /// and on control-channel retirement. Failed worker startup can invoke it
    /// during cleanup on the starting caller. It must only signal the caller,
    /// return promptly, and never run its handler or wait for notification capacity.
    /// A rejected/coalesced wake is safe when the caller reconciles retained work
    /// between dispatches and before its one blocking wait.
    pub fn start_on_with_wake(
        self,
        device: &'static impl gnss::Device,
        wake: impl FnMut() + Send + 'static,
    ) -> io::Result<GnssHandle> {
        let (control, inbox) = bounded(INBOX_CAPACITY);
        // This endpoint only admits messages; acquisition invokes it in task context.
        let sink = crate::inbox_sink(control.clone(), |event| Some(Event::Device(event)));
        let device = device.open(sink).map_err(hal_error)?;
        Self::start_device(self, device, control, inbox, wake)
    }

    #[cfg(test)]
    pub(super) fn start_with_test_device<D: Session + Send + 'static>(
        self,
        device: D,
    ) -> io::Result<(GnssHandle, TestInput)> {
        let (control, inbox) = bounded(INBOX_CAPACITY);
        let inject = TestInput(control.clone());
        let handle = Self::start_device(self, device, control, inbox, || {})?;
        Ok((handle, inject))
    }
}

#[cfg(test)]
impl TestInput {
    pub(super) fn fix(&self, fix: GnssFix) -> Result<(), ()> {
        self.0
            .try_send(Event::Device(gnss::Event::Fix(fix)))
            .map_err(|_| ())
    }
}

impl GnssService {
    fn start_device<D: Session + Send + 'static>(
        self,
        device: D,
        control: Sender<Event>,
        inbox: Inbox<Event>,
        wake: impl FnMut() + Send + 'static,
    ) -> io::Result<GnssHandle> {
        let stopping = Arc::new(AtomicBool::new(false));
        let stop = stopping.clone();
        let (replies, output) = control::channel(wake);
        let join = thread::Builder::new()
            .name("gnss-service".into())
            .spawn(move || run(self, device, inbox, &stop, output))?;
        Ok(GnssHandle {
            control,
            replies,
            join: Some(join),
            stopping,
        })
    }
}
impl GnssHandle {
    /// Admit one command without waiting for inbox space or service completion.
    ///
    /// A successful admission occupies this handle's retained request slot until
    /// `try_completion` consumes its result. Caller deadline expiry does not
    /// cancel the operation or free that slot. No channel is created per request.
    /// This one-outstanding limit applies to this API; the blocking compatibility
    /// methods admit their own separate requests.
    pub fn try_request(&mut self, request: ControlRequest) -> Result<RequestId, RequestError> {
        self.replies
            .try_request(&self.control, request, Event::Control)
    }

    /// Consume a retained completion without waiting. Reconcile this between
    /// dispatches, including after unrelated or stale wakes.
    pub fn try_completion(&mut self) -> Option<ControlCompletion<GnssStatus>> {
        self.replies.try_completion()
    }

    /// Blocking convenience for bootstrap/CLI callers, never an owner handler.
    pub fn pause(&self) -> io::Result<()> {
        self.request(Event::Pause)
    }
    /// Blocking convenience for bootstrap/CLI callers, never an owner handler.
    pub fn resume(&self) -> io::Result<()> {
        self.request(Event::Resume)
    }
    /// Blocking convenience for bootstrap/CLI callers, never an owner handler.
    pub fn status(&self) -> io::Result<GnssStatus> {
        let (reply, response) = sync_channel(1);
        self.send(Event::Status(reply))?;
        response
            .recv()
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "gnss status reply closed"))
    }
    /// Blocking joined teardown after the caller has resolved progress dependencies.
    pub fn stop(mut self) -> io::Result<GnssStats> {
        self.request_stop();
        self.join
            .take()
            .expect("owned join")
            .join()
            .map_err(|_| io::Error::other("gnss service panicked"))?
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
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "gnss command reply closed"))
    }
    fn send(&self, event: Event) -> io::Result<()> {
        self.control
            .send(event)
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "gnss service stopped"))
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
    io::Error::new(kind, format!("Gnss HAL: {error:?}"))
}
impl Drop for GnssHandle {
    fn drop(&mut self) {
        self.request_stop();
    }
}
fn run<D: Session>(
    mut service: GnssService,
    mut device: D,
    inbox: Inbox<Event>,
    stopping: &AtomicBool,
    mut replies: control::Replies<GnssStatus, impl FnMut()>,
) -> io::Result<GnssStats> {
    let mut stats = GnssStats::default();
    let mut sampling = true;
    let mut fault = None;
    while !stopping.load(Ordering::Acquire) {
        let Ok(event) = inbox.wait(None) else {
            break;
        };
        match event {
            Event::Control(ticket) => {
                match ticket.request {
                    ControlRequest::Pause => sampling = false,
                    ControlRequest::Resume => sampling = true,
                    ControlRequest::Status => {}
                }
                let mut snapshot = stats;
                snapshot.dropped = snapshot.dropped.saturating_add(u64::from(device.dropped()));
                replies.complete(
                    ticket,
                    GnssStatus {
                        sampling,
                        stats: snapshot,
                    },
                );
            }
            Event::Pause(reply) => {
                sampling = false;
                let _ = reply.try_send(());
            }
            Event::Resume(reply) => {
                sampling = true;
                let _ = reply.try_send(());
            }
            Event::Status(reply) => {
                let mut snapshot = stats;
                snapshot.dropped = snapshot.dropped.saturating_add(u64::from(device.dropped()));
                let _ = reply.try_send(GnssStatus {
                    sampling,
                    stats: snapshot,
                });
            }
            Event::Device(gnss::Event::Fix(value)) => {
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
            Event::Device(gnss::Event::Satellites(_) | gnss::Event::NoFix(_)) => {}
            Event::Device(gnss::Event::InvalidFrame) => {
                stats.errors = stats.errors.saturating_add(1);
            }
            Event::Device(gnss::Event::Failed(error)) => {
                fault = Some(error);
                break;
            }
            Event::Shutdown => break,
        }
    }
    // Retire control before potentially blocking provider cleanup. Pending
    // requests fail visibly; the caller still joins teardown explicitly.
    drop(replies);
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
