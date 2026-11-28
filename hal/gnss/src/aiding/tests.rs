use super::*;
use peripheral::gnss::Report;
use std::{
    io,
    sync::atomic::{AtomicBool, Ordering},
};

struct Signal(AtomicBool);
impl Wake for Signal {
    fn wake(&self) -> io::Result<()> {
        if self.0.load(Ordering::Acquire) {
            Err(io::Error::other("wake"))
        } else {
            Ok(())
        }
    }
}
struct Source {
    steps: std::collections::VecDeque<Progress>,
    started: usize,
}
impl Receiver for Source {
    fn configure(&mut self, _: Instant) -> Result<(), Error> {
        Ok(())
    }
    fn restore(&mut self, _: Instant) -> Result<(), Error> {
        Ok(())
    }
    fn read_available(&mut self, _: impl FnMut(Result<Report<'_>, Error>)) -> Result<(), Error> {
        Ok(())
    }
    fn begin_aiding(&mut self, aid: Aid, now_us: u64, _: Instant) -> Result<(), SubmitError> {
        aid.validate(now_us)?;
        self.started += 1;
        Ok(())
    }
    fn advance_aiding(&mut self, _: Instant, _: u64) -> Result<Progress, Error> {
        Ok(self.steps.pop_front().expect("one bounded protocol step"))
    }
}
fn hint() -> Aid {
    Aid::Time(TimeAid {
        unix_time_ms: 1_800_000_000_000,
        observed_at_us: acquisition::monotonic_us(),
        uncertainty: Duration::from_millis(20),
        max_age: Duration::from_secs(60),
        drift_ppm: 20,
    })
}
fn source(steps: impl IntoIterator<Item = Progress>) -> Source {
    Source {
        steps: steps.into_iter().collect(),
        started: 0,
    }
}
fn advance(owner: &mut Owner, source: &mut Source) -> bool {
    owner
        .advance(source, Instant::now(), acquisition::monotonic_us())
        .unwrap()
}

#[test]
fn one_request_retains_rejection_until_consumed_even_after_retirement() {
    let (mut handle, mut owner) = channel(
        Capabilities {
            time: true,
            position: false,
        },
        Arc::new(Signal(AtomicBool::new(false))),
    );
    let outcome = Ok(Outcome::Rejected(Rejection::NotReady));
    let mut receiver = source([
        Progress::Writing,
        Progress::AwaitingAck,
        Progress::Complete(outcome),
    ]);
    let id = handle.try_submit(hint()).unwrap();
    assert_eq!(handle.try_submit(hint()), Err(SubmitError::Busy));
    assert!(!advance(&mut owner, &mut receiver));
    assert!(owner.writable());
    assert!(!advance(&mut owner, &mut receiver));
    assert!(!owner.writable());
    assert!(advance(&mut owner, &mut receiver));
    assert_eq!(handle.try_submit(hint()), Err(SubmitError::Busy));
    owner.close(Ok(Outcome::Cancelled));
    assert_eq!(
        handle.try_completion(),
        Some(Completion {
            id,
            result: outcome
        })
    );
    assert_eq!(handle.try_completion(), None);
    assert_eq!(handle.try_submit(hint()), Err(SubmitError::Stopped));
}

#[test]
fn failed_wake_rolls_back_admission_and_does_not_consume_an_id() {
    let signal = Arc::new(Signal(AtomicBool::new(true)));
    let (mut handle, mut owner) = channel(
        Capabilities {
            time: true,
            position: false,
        },
        signal.clone(),
    );
    let mut receiver = source([]);
    assert_eq!(handle.try_submit(hint()), Err(SubmitError::Io));
    assert!(!advance(&mut owner, &mut receiver));
    assert_eq!(receiver.started, 0);
    signal.0.store(false, Ordering::Release);
    assert_eq!(handle.try_submit(hint()), Ok(RequestId(1)));
    drop(owner);
    assert_eq!(
        handle.try_completion().unwrap().result,
        Ok(Outcome::Cancelled)
    );
}

#[test]
fn submission_and_owner_contention_are_bounded_and_rearm_progress() {
    let (mut handle, mut owner) = channel(
        Capabilities {
            time: true,
            position: false,
        },
        Arc::new(Signal(AtomicBool::new(false))),
    );
    let shared = handle.shared.clone();
    let guard = shared.slot.lock().unwrap();
    assert_eq!(handle.try_submit(hint()), Err(SubmitError::Busy));
    drop(guard);
    handle.try_submit(hint()).unwrap();
    let mut receiver = source([Progress::Complete(Ok(Outcome::Accepted))]);
    let guard = shared.slot.lock().unwrap();
    assert!(!advance(&mut owner, &mut receiver));
    assert!(owner.deadline().is_some());
    assert_eq!(receiver.started, 0);
    drop(guard);
    assert!(advance(&mut owner, &mut receiver));
    assert_eq!(
        handle.try_completion().unwrap().result,
        Ok(Outcome::Accepted)
    );
}

#[test]
fn queued_expiry_is_nonfatal_and_stop_rejects_new_requests_immediately() {
    let (mut handle, mut owner) = channel(
        Capabilities {
            time: true,
            position: false,
        },
        Arc::new(Signal(AtomicBool::new(false))),
    );
    let aid = hint();
    let Aid::Time(time) = aid else { unreachable!() };
    handle.try_submit(aid).unwrap();
    let mut receiver = source([]);
    assert!(owner
        .advance(
            &mut receiver,
            Instant::now(),
            time.observed_at_us + 60_000_001
        )
        .unwrap());
    assert_eq!(
        handle.try_completion().unwrap().result,
        Err(SubmitError::Expired)
    );
    assert_eq!(receiver.started, 0);
    handle.try_submit(hint()).unwrap();
    owner.stop_handle().request();
    owner.close(Ok(Outcome::Cancelled));
    assert_eq!(
        handle.try_completion().unwrap().result,
        Ok(Outcome::Cancelled)
    );
    assert_eq!(handle.try_submit(hint()), Err(SubmitError::Stopped));
}

#[test]
fn handle_is_send_sync_and_unsupported_mock_is_honest() {
    fn send_sync<T: Send + Sync>() {}
    send_sync::<AidingHandle>();
    let mut session: Box<dyn crate::Session> =
        Box::new(crate::mock_tests::open(|_| Ok(())).unwrap());
    assert!(session.aiding().is_none());
    session.join().unwrap();
}

#[test]
fn retired_client_and_stop_retain_results_without_retaining_wake_resources() {
    struct SignalCount(Arc<std::sync::atomic::AtomicUsize>);
    impl Wake for SignalCount {
        fn wake(&self) -> io::Result<()> {
            self.0.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
    }
    let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let signal = Arc::new(SignalCount(calls.clone()));
    let lifetime = Arc::downgrade(&signal);
    let (mut handle, mut owner) = channel(
        Capabilities {
            time: true,
            position: false,
        },
        signal,
    );
    let stop = owner.stop_handle();
    let id = handle.try_submit(hint()).unwrap();
    assert_eq!(calls.load(Ordering::Relaxed), 1);
    let mut receiver = source([Progress::Complete(Ok(Outcome::Accepted))]);
    assert!(advance(&mut owner, &mut receiver));
    drop(owner);
    assert!(lifetime.upgrade().is_none());
    stop.request();
    assert_eq!(
        handle.try_completion(),
        Some(Completion {
            id,
            result: Ok(Outcome::Accepted)
        })
    );
    assert_eq!(handle.try_submit(hint()), Err(SubmitError::Stopped));
    assert_eq!(calls.load(Ordering::Relaxed), 1);
}
