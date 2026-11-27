use acquisition::{Wake, Worker};
use std::{
    io,
    sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc, Arc,
    },
    time::{Duration, Instant},
};

struct Cancel(Arc<AtomicUsize>);
impl Wake for Cancel {
    fn wake(&self) -> io::Result<()> {
        let count = self.0.fetch_add(1, Ordering::Relaxed);
        Err(io::Error::from(if count == 0 {
            io::ErrorKind::Interrupted
        } else {
            io::ErrorKind::BrokenPipe
        }))
    }
}

#[test]
fn failed_wake_preserves_running_handle_and_eventual_terminal_fault() {
    let (release, wait) = mpsc::sync_channel(1);
    let calls = Arc::new(AtomicUsize::new(0));
    let mut worker = Worker::start(
        "join-test",
        65536,
        Cancel(calls.clone()),
        (),
        move |_, ()| {
            wait.recv_timeout(Duration::from_secs(2)).unwrap();
            Err(7u8)
        },
    )
    .unwrap();
    assert_eq!(worker.stop_and_join(9), Err(9));
    assert_eq!(calls.load(Ordering::Relaxed), 2); // Interrupted was retried.
    assert!(!worker.is_finished());
    release.send(()).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    while !worker.is_finished() && Instant::now() < deadline {
        std::thread::yield_now();
    }
    assert!(worker.is_finished());
    assert_eq!(worker.stop_and_join(9), Err(7));
    assert_eq!(worker.stop_and_join(9), Err(7));
    drop(worker);
    assert_eq!(calls.load(Ordering::Relaxed), 2); // Completed work never needs another wake.
}

struct Interruptions {
    calls: Arc<AtomicUsize>,
    before_success: usize,
}

impl Wake for Interruptions {
    fn wake(&self) -> io::Result<()> {
        if self.calls.fetch_add(1, Ordering::Relaxed) < self.before_success {
            Err(io::ErrorKind::Interrupted.into())
        } else {
            Ok(())
        }
    }
}

#[test]
fn persistent_interruption_returns_to_supervision_with_stop_and_owner_retained() {
    let calls = Arc::new(AtomicUsize::new(0));
    let (release, held) = mpsc::sync_channel(1);
    let worker = Worker::start(
        "interrupted-stop",
        65536,
        Interruptions {
            calls: calls.clone(),
            before_success: usize::MAX,
        },
        (),
        move |stop, ()| {
            held.recv_timeout(Duration::from_secs(2)).unwrap();
            assert!(
                stop.load(Ordering::Acquire),
                "wake failure must not clear cancellation"
            );
            Err(7u8)
        },
    )
    .unwrap();
    let (returned, received) = mpsc::sync_channel(1);
    let caller = std::thread::spawn(move || {
        let result = worker.request_stop();
        let _ = returned.send((worker, result));
    });
    let (mut worker, result) = received
        .recv_timeout(Duration::from_secs(1))
        .expect("a persistently interrupted wake must not strand the supervisor");
    caller.join().unwrap();
    assert_eq!(result.unwrap_err().kind(), io::ErrorKind::Interrupted);
    assert_eq!(calls.load(Ordering::Relaxed), 3);
    assert!(!worker.is_finished());
    release.send(()).unwrap();
    // A direct join observes the task's retained fault, not the earlier wake error.
    assert_eq!(worker.join(9), Err(7));
    assert!(worker.is_finished());
    assert_eq!(calls.load(Ordering::Relaxed), 3);
}

#[test]
fn transient_interruptions_can_complete_the_wake_with_cancellation_retained() {
    let calls = Arc::new(AtomicUsize::new(0));
    let (release, held) = mpsc::sync_channel(1);
    let mut worker = Worker::start(
        "transient-stop",
        65536,
        Interruptions {
            calls: calls.clone(),
            before_success: 2,
        },
        (),
        move |stop, ()| {
            held.recv_timeout(Duration::from_secs(2)).unwrap();
            assert!(stop.load(Ordering::Acquire));
            Ok::<(), u8>(())
        },
    )
    .unwrap();
    worker.request_stop().unwrap();
    assert_eq!(calls.load(Ordering::Relaxed), 3);
    release.send(()).unwrap();
    assert_eq!(worker.join(9), Ok(()));
}
