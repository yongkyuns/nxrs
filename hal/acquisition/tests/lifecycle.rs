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
fn retained_owner_outlives_sink_even_when_callback_panics() {
    struct Released(u8, Arc<std::sync::Mutex<Vec<u8>>>);
    impl Drop for Released {
        fn drop(&mut self) {
            self.1.lock().unwrap().push(self.0);
        }
    }
    for panic in [false, true] {
        let order = Arc::new(std::sync::Mutex::new(Vec::new()));
        let sink = Released(1, order.clone());
        let owner = Released(2, order.clone());
        let mut worker = Worker::start_guarded(
            "retirement-test",
            65536,
            Cancel(Arc::new(AtomicUsize::new(0))),
            move |_: u8| {
                let _retained_sink = &sink;
                assert!(!panic, "injected callback panic");
                Ok(())
            },
            owner,
            |e| e,
            |_, emit| {
                emit(1);
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(worker.join(9), if panic { Err(9) } else { Ok(()) });
        assert_eq!(*order.lock().unwrap(), [1, 2]);
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
        |_| Ok(()),
        |e| e,
        move |_, _| {
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
