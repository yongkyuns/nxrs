//! Isolated host witness for the shared acquisition worker's Rust allocations.
//!
//! The real Worker and readiness::Cancel own their ordinary resources. Safe
//! host pipes supply one fixture input and cancellation; this does not exercise
//! NuttX's nonblocking pipe2/poll ABI or establish that an OS wait was parked.
#![forbid(unsafe_code)]

#[cfg(unix)]
mod unix {
    use acquisition::{readiness::Cancel, Worker};
    use nxrs_allocation_probe::{begin_measurement, Snapshot, Tracking};
    use std::{
        alloc::System,
        fs::File,
        io::{self, Read, Write},
        os::fd::OwnedFd,
        sync::atomic::{AtomicU8, AtomicUsize, Ordering},
        time::{Duration, Instant},
    };

    #[global_allocator]
    static ALLOCATOR: Tracking<System> = Tracking::new(System);

    // A host stack request, not a measured allocation or a target stack budget.
    const STACK_BYTES: usize = 64 * 1024;
    const INPUT: u8 = 0x5a;
    const LIFECYCLES: usize = 9;
    const CASES: [&str; 2 * LIFECYCLES] = [
        "one-worker",
        "one-worker-repeat-1",
        "one-worker-repeat-2",
        "one-worker-repeat-3",
        "one-worker-repeat-4",
        "one-worker-repeat-5",
        "one-worker-repeat-6",
        "one-worker-repeat-7",
        "one-worker-repeat-8",
        "two-workers",
        "two-workers-repeat-1",
        "two-workers-repeat-2",
        "two-workers-repeat-3",
        "two-workers-repeat-4",
        "two-workers-repeat-5",
        "two-workers-repeat-6",
        "two-workers-repeat-7",
        "two-workers-repeat-8",
    ];

    struct Lane {
        reached: AtomicU8,
        permitted: AtomicU8,
        delivered: AtomicUsize,
    }

    impl Lane {
        const fn new() -> Self {
            Self {
                reached: AtomicU8::new(0),
                permitted: AtomicU8::new(0),
                delivered: AtomicUsize::new(0),
            }
        }

        fn reset(&self) {
            // The preceding lifecycle has joined and dropped every worker.
            self.reached.store(0, Ordering::Relaxed);
            self.permitted.store(0, Ordering::Relaxed);
            self.delivered.store(0, Ordering::Relaxed);
        }
    }

    static LANES: [Lane; 2] = [Lane::new(), Lane::new()];

    #[derive(Clone, Copy, Debug)]
    enum Failure {
        Input,
        Delivery,
        Cancellation,
        Join,
    }

    #[derive(Clone, Copy)]
    struct Measurement {
        before: Snapshot,
        construction: Snapshot,
        first_wait: Snapshot,
        stop_join: Snapshot,
        session_drop: Snapshot,
    }

    impl Measurement {
        fn report(self, case: &str) {
            for (before, after, phase) in [
                (self.before, self.construction, "construction"),
                (self.construction, self.first_wait, "first-wait"),
                (self.first_wait, self.stop_join, "stop-join"),
                (self.stop_join, self.session_drop, "session-drop"),
                (self.before, self.session_drop, "reclaimed"),
            ] {
                after.report(before, "acquisition", case, phase);
            }
        }
    }

    fn pipe_files() -> io::Result<(File, File)> {
        let (reader, writer) = io::pipe()?;
        Ok((
            File::from(OwnedFd::from(reader)),
            File::from(OwnedFd::from(writer)),
        ))
    }

    fn wait_for(value: &AtomicU8, expected: u8) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while value.load(Ordering::Acquire) != expected {
            assert!(Instant::now() < deadline, "fixture phase timed out");
            std::thread::yield_now();
        }
    }

    fn lifecycle<const N: usize>() -> Measurement {
        assert!(N == 1 || N == 2);
        for lane in &LANES[..N] {
            lane.reset();
        }
        let before = ALLOCATOR.snapshot();
        let mut sessions: [(Worker<Cancel, Failure>, File); N] = std::array::from_fn(|index| {
            let lane = &LANES[index];
            // Fixture input is separate from the real cancellation owner. The
            // two host pipe pairs' native storage is outside Rust counters.
            let (mut input, input_writer) = pipe_files().unwrap();
            let cancel = Cancel::new(pipe_files).unwrap();
            let cancel_reader = cancel.reader.clone();
            let worker = Worker::start(
                "allocation-witness",
                STACK_BYTES,
                cancel,
                move |value: u8| {
                    assert_eq!(value, INPUT);
                    lane.delivered.fetch_add(1, Ordering::Relaxed);
                    Ok(())
                },
                |_| 0,
                move |stopping, emit| {
                    // Acknowledge entry to user code before construction's
                    // snapshot; no channels or heap-backed gates are involved.
                    lane.reached.store(1, Ordering::Release);
                    wait_for(&lane.permitted, 1);
                    let mut byte = [0];
                    input.read_exact(&mut byte).map_err(|_| Failure::Input)?;
                    if byte[0] != INPUT {
                        return Err(Failure::Input);
                    }
                    if !emit(byte[0]) {
                        return Err(Failure::Delivery);
                    }
                    // The first read AND nonallocating sink have completed.
                    // This is stronger than merely announcing wait entry; the
                    // byte may nevertheless have arrived before the OS read.
                    lane.reached.store(2, Ordering::Release);
                    wait_for(&lane.permitted, 2);
                    (&*cancel_reader)
                        .read_exact(&mut byte)
                        .map_err(|_| Failure::Cancellation)?;
                    if byte[0] != 1 || !stopping.load(Ordering::Acquire) {
                        return Err(Failure::Cancellation);
                    }
                    Ok(())
                },
            )
            .unwrap();
            (worker, input_writer)
        });
        for lane in &LANES[..N] {
            wait_for(&lane.reached, 1);
        }
        let construction = ALLOCATOR.snapshot();
        construction.validate_after(before).unwrap();
        assert!(construction.alloc > before.alloc || construction.zeroed > before.zeroed);
        assert!(construction.live > before.live);
        assert!(construction.blocks > before.blocks);

        for (index, (_, writer)) in sessions.iter_mut().enumerate() {
            LANES[index].permitted.store(1, Ordering::Release);
            writer.write_all(&[INPUT]).unwrap();
        }
        for lane in &LANES[..N] {
            wait_for(&lane.reached, 2);
            assert_eq!(lane.delivered.load(Ordering::Relaxed), 1);
        }
        let first_wait = ALLOCATOR.snapshot();
        first_wait.no_allocator_calls_since(construction).unwrap();

        for lane in &LANES[..N] {
            lane.permitted.store(2, Ordering::Release);
        }
        for (worker, _) in &mut sessions {
            worker.stop_and_join(Failure::Join).unwrap();
            assert!(worker.is_finished());
            assert_eq!(worker.dropped(), 0);
        }
        let stop_join = ALLOCATOR.snapshot();
        stop_join.validate_after(first_wait).unwrap();
        // Worker still owns Arc<State> and Cancel still owns Arc<File>. Join
        // ends worker activity; it is not the session's reclamation boundary.
        assert!(stop_join.live > before.live);
        assert!(stop_join.blocks > before.blocks);

        drop(sessions);
        let session_drop = ALLOCATOR.snapshot();
        session_drop.validate_after(stop_join).unwrap();
        assert_eq!(session_drop.alloc, stop_join.alloc);
        assert_eq!(session_drop.zeroed, stop_join.zeroed);
        assert_eq!(session_drop.realloc, stop_join.realloc);
        assert!(session_drop.dealloc > stop_join.dealloc);
        session_drop.reclaimed_since(before).unwrap();
        Measurement {
            before,
            construction,
            first_wait,
            stop_join,
            session_drop,
        }
    }

    fn observer_control() {
        let before = ALLOCATOR.snapshot();
        let mut retained = vec![0x5a; 257];
        std::hint::black_box(&mut retained);
        let live = ALLOCATOR.snapshot();
        live.validate_after(before).unwrap();
        assert!(live.alloc > before.alloc || live.zeroed > before.zeroed);
        assert!(live.live > before.live && live.blocks > before.blocks);
        assert!(live.no_allocator_calls_since(before).is_err());
        assert!(live.reclaimed_since(before).is_err());
        drop(retained);
        ALLOCATOR.snapshot().reclaimed_since(before).unwrap();
    }

    pub fn run() {
        begin_measurement();
        observer_control();
        // Fixed counts, with no discarded warmup, byte allowance, or adaptive
        // baseline. Keep every result on the stack until all measurements end.
        let one: [Measurement; LIFECYCLES] = std::array::from_fn(|_| lifecycle::<1>());
        let two: [Measurement; LIFECYCLES] = std::array::from_fn(|_| lifecycle::<2>());
        for family in [&one, &two] {
            for measurement in family {
                measurement.before.reclaimed_since(one[0].before).unwrap();
                measurement
                    .session_drop
                    .reclaimed_since(one[0].before)
                    .unwrap();
            }
        }
        for (case, measurement) in CASES.into_iter().zip(one.into_iter().chain(two)) {
            measurement.report(case);
        }
        println!("MEMORY_CONTROL app=acquisition kind=allocation-and-retention rejected=true");
        println!("MEMORY_PASS app=acquisition cases={}", CASES.len());
    }
}

#[cfg(unix)]
fn main() {
    unix::run();
}

#[cfg(not(unix))]
fn main() {
    eprintln!("acquisition allocation witness requires a Unix host with descriptor pipes");
    std::process::exit(1);
}
