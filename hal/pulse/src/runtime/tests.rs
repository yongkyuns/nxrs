use super::*;
use std::{
    collections::VecDeque,
    io,
    sync::{atomic::AtomicUsize, mpsc, Condvar},
    time::Duration,
};

const WATCHDOG: Duration = Duration::from_secs(5);

#[derive(Default)]
struct Script {
    pending: VecDeque<Result<Instant, Error>>,
    cancelled: bool,
    waiting: bool,
}
#[derive(Default)]
struct Control {
    script: Mutex<Script>,
    changed: Condvar,
    finished: AtomicBool,
    fail_cancel: AtomicBool,
    fail_cleanup: AtomicBool,
    fail_disable: AtomicBool,
    fail_configure: AtomicBool,
    enabled: AtomicBool,
    output_dropped: AtomicBool,
    configure_calls: AtomicUsize,
}
impl Control {
    fn send(&self, step: Result<Instant, Error>) {
        locked(&self.script).pending.push_back(step);
        self.changed.notify_all();
    }
    fn wait_until_blocked(&self) {
        let state = locked(&self.script);
        let (state, timeout) = self
            .changed
            .wait_timeout_while(state, WATCHDOG, |state| !state.waiting)
            .unwrap();
        assert!(
            !timeout.timed_out() && state.waiting,
            "source never entered its blocking wait"
        );
    }
}

struct TestInput {
    control: Arc<Control>,
    lease: Arc<Lease>,
}
impl Input for TestInput {
    fn next(&mut self) -> Result<Option<Instant>, Error> {
        let mut state = locked(&self.control.script);
        state.waiting = true;
        self.control.changed.notify_all();
        while state.pending.is_empty() && !state.cancelled {
            state = self.control.changed.wait(state).unwrap();
        }
        state.waiting = false;
        if state.cancelled {
            Ok(None)
        } else {
            state.pending.pop_front().unwrap().map(Some)
        }
    }
    fn finish(&mut self) -> Result<(), Error> {
        self.control.finished.store(true, Ordering::Release);
        if self.control.fail_cleanup.load(Ordering::Acquire) {
            self.lease.quarantine();
            Err(Error::Shutdown)
        } else {
            Ok(())
        }
    }
}

struct TestCancel(Arc<Control>);
impl Wake for TestCancel {
    fn wake(&self) -> io::Result<()> {
        if self.0.fail_cancel.swap(false, Ordering::AcqRel) {
            return Err(io::Error::other("injected cancellation failure"));
        }
        locked(&self.0.script).cancelled = true;
        self.0.changed.notify_all();
        Ok(())
    }
}

struct TestOutput {
    control: Arc<Control>,
    lease: Arc<Lease>,
}
impl Output for TestOutput {
    fn configure(&mut self, _: Waveform) -> Result<(), Error> {
        if self.lease.is_quarantined() {
            return Err(Error::Shutdown);
        }
        self.control.configure_calls.fetch_add(1, Ordering::Relaxed);
        // A failed hardware operation can already have changed output.
        self.control.enabled.store(true, Ordering::Release);
        if self.control.fail_configure.swap(false, Ordering::AcqRel) {
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
    fn disable(&mut self) -> Result<(), Error> {
        if self.control.fail_disable.swap(false, Ordering::AcqRel) {
            return Err(Error::Shutdown);
        }
        self.control.enabled.store(false, Ordering::Release);
        Ok(())
    }
    fn quarantine(&mut self) {
        self.lease.quarantine();
    }
}
impl Drop for TestOutput {
    fn drop(&mut self) {
        self.control.output_dropped.store(true, Ordering::Release);
    }
}

fn fixture<G: Send + 'static>(
    admission: &'static Admission,
    sink: impl FnMut(Event) -> bool + Send + 'static,
    guard: impl FnOnce(Arc<Lease>) -> G,
) -> (Managed<TestOutput, TestCancel>, Arc<Control>) {
    let lease = admission.acquire().unwrap();
    let control = Arc::new(Control::default());
    let session = start(
        TestOutput {
            control: control.clone(),
            lease: lease.clone(),
        },
        TestInput {
            control: control.clone(),
            lease: lease.clone(),
        },
        TestCancel(control.clone()),
        guard(lease),
        32768,
        sink,
    )
    .unwrap();
    control.wait_until_blocked();
    (session, control)
}

#[test]
fn rejected_edges_and_rejected_terminal_fault_are_retained_without_retry() {
    static ADMISSION: Admission = Admission::new();
    let (events, incoming) = mpsc::channel();
    let (mut session, control) = fixture(
        &ADMISSION,
        move |event| {
            events.send(event).unwrap();
            matches!(event, Event::Edge { sequence: 2, .. })
        },
        |lease| lease,
    );
    let received = Instant::now();
    control.send(Ok(received));
    control.send(Ok(received));
    control.send(Err(Error::Io));
    assert_eq!(
        incoming.recv_timeout(WATCHDOG).unwrap(),
        Event::Edge {
            sequence: 1,
            received
        }
    );
    assert_eq!(
        incoming.recv_timeout(WATCHDOG).unwrap(),
        Event::Edge {
            sequence: 2,
            received
        }
    );
    assert_eq!(
        incoming.recv_timeout(WATCHDOG).unwrap(),
        Event::Fault(Error::Io)
    );
    assert_eq!(
        session.stats(),
        Stats {
            observed: 2,
            rejected: 1,
            fault: Some(Error::Io)
        }
    );
    assert_eq!(
        session.configure(Waveform::new(25, 50).unwrap()),
        Err(Error::Io)
    );
    assert_eq!(session.shutdown(), Err(Error::Io));
    assert!(incoming.try_recv().is_err(), "rejected event was retried");
    assert!(control.finished.load(Ordering::Acquire));
    assert!(control.output_dropped.load(Ordering::Acquire));
    assert!(
        ADMISSION.acquire().is_ok(),
        "cleaned source fault prevented reacquisition"
    );
}

#[test]
fn cancellation_joins_blocked_input_and_releases_resources_before_return() {
    static ADMISSION: Admission = Admission::new();
    let (mut session, control) = fixture(&ADMISSION, |_| true, |lease| lease);
    assert!(matches!(ADMISSION.acquire(), Err(Error::Busy)));
    assert!(
        !control.enabled.load(Ordering::Acquire),
        "open must start disabled"
    );
    session.configure(Waveform::new(25, 50).unwrap()).unwrap();
    session.disable().unwrap();
    session.configure(Waveform::new(10, 25).unwrap()).unwrap();
    assert_eq!(session.shutdown(), Ok(Stats::default()));
    assert_eq!(session.shutdown(), Ok(Stats::default()));
    assert_eq!(
        session.configure(Waveform::new(25, 50).unwrap()),
        Err(Error::Closed)
    );
    assert!(control.finished.load(Ordering::Acquire));
    assert!(!control.enabled.load(Ordering::Acquire));
    assert!(control.output_dropped.load(Ordering::Acquire));
    assert!(ADMISSION.acquire().is_ok());
}

#[test]
fn invalid_waveform_does_not_enter_output_and_failed_write_still_can_be_stopped() {
    static ADMISSION: Admission = Admission::new();
    let (mut session, control) = fixture(&ADMISSION, |_| true, |lease| lease);
    assert_eq!(Waveform::new(0, 50), Err(Error::InvalidConfig));
    assert_eq!(control.configure_calls.load(Ordering::Relaxed), 0);
    control.fail_configure.store(true, Ordering::Release);
    assert_eq!(
        session.configure(Waveform::new(25, 50).unwrap()),
        Err(Error::Io)
    );
    assert!(
        control.enabled.load(Ordering::Acquire),
        "failure fixture must have a partial effect"
    );
    session.disable().unwrap();
    assert!(!control.enabled.load(Ordering::Acquire));
    session.shutdown().unwrap();
}

#[test]
fn failed_output_stop_still_cancels_input_and_retains_output_for_retry() {
    static ADMISSION: Admission = Admission::new();
    let (mut session, control) = fixture(&ADMISSION, |_| true, |lease| lease);
    session.configure(Waveform::new(25, 50).unwrap()).unwrap();
    control.fail_disable.store(true, Ordering::Release);
    assert_eq!(session.shutdown(), Err(Error::Shutdown));
    assert!(
        control.finished.load(Ordering::Acquire),
        "output error skipped source cleanup"
    );
    assert!(!control.output_dropped.load(Ordering::Acquire));
    assert!(matches!(ADMISSION.acquire(), Err(Error::Busy)));
    // The successful retry releases resources but preserves the earlier fault.
    assert_eq!(session.shutdown(), Err(Error::Shutdown));
    assert!(control.output_dropped.load(Ordering::Acquire));
    assert!(!control.enabled.load(Ordering::Acquire));
    assert!(ADMISSION.acquire().is_ok());
}

#[test]
fn failed_cancel_returns_without_joining_a_blocked_worker_and_can_retry() {
    static ADMISSION: Admission = Admission::new();
    let (mut session, control) = fixture(&ADMISSION, |_| true, |lease| lease);
    control.fail_cancel.store(true, Ordering::Release);
    assert_eq!(session.shutdown(), Err(Error::Shutdown));
    assert!(!control.finished.load(Ordering::Acquire));
    assert!(!control.output_dropped.load(Ordering::Acquire));
    assert!(matches!(ADMISSION.acquire(), Err(Error::Busy)));
    assert_eq!(session.shutdown(), Err(Error::Shutdown));
    assert!(control.finished.load(Ordering::Acquire));
    assert!(ADMISSION.acquire().is_ok());
}

#[test]
fn failed_source_cleanup_quarantines_reacquisition() {
    static ADMISSION: Admission = Admission::new();
    let (mut session, control) = fixture(&ADMISSION, |_| false, |lease| lease);
    control.fail_cleanup.store(true, Ordering::Release);
    assert_eq!(session.shutdown(), Err(Error::Shutdown));
    assert_eq!(session.stats().fault, Some(Error::Shutdown));
    assert!(control.finished.load(Ordering::Acquire));
    drop(session);
    assert!(matches!(ADMISSION.acquire(), Err(Error::Shutdown)));
}

#[test]
fn drop_retains_exclusive_admission_through_sink_destruction_without_joining() {
    static ADMISSION: Admission = Admission::new();
    struct SinkLifetime {
        entered: mpsc::Sender<()>,
        release: mpsc::Receiver<()>,
    }
    impl Drop for SinkLifetime {
        fn drop(&mut self) {
            self.entered.send(()).unwrap();
            self.release.recv_timeout(WATCHDOG).unwrap();
        }
    }
    struct Retired {
        lease: Option<Arc<Lease>>,
        done: mpsc::Sender<()>,
    }
    impl Drop for Retired {
        fn drop(&mut self) {
            self.lease.take();
            self.done.send(()).unwrap();
        }
    }
    let (entered, waiting) = mpsc::channel();
    let (release, allowed) = mpsc::channel();
    let (done, retired) = mpsc::channel();
    let lifetime = SinkLifetime {
        entered,
        release: allowed,
    };
    let (session, control) = fixture(
        &ADMISSION,
        move |_| {
            let _ = &lifetime;
            true
        },
        |lease| Retired {
            lease: Some(lease),
            done,
        },
    );
    drop(session);
    waiting.recv_timeout(WATCHDOG).unwrap();
    assert!(control.finished.load(Ordering::Acquire));
    assert!(control.output_dropped.load(Ordering::Acquire));
    assert!(matches!(ADMISSION.acquire(), Err(Error::Busy)));
    release.send(()).unwrap();
    retired.recv_timeout(WATCHDOG).unwrap();
    assert!(ADMISSION.acquire().is_ok());
}
