#![feature(mpmc_channel)]
use std::{
    sync::{
        atomic::{AtomicUsize, Ordering::SeqCst},
        mpmc, mpsc, Arc, Barrier, Mutex,
    },
    thread,
    time::Duration,
};
#[derive(Debug)]
struct Message {
    id: usize,
    drops: Arc<AtomicUsize>,
}
impl Drop for Message {
    fn drop(&mut self) {
        self.drops.fetch_add(1, SeqCst);
    }
}
fn capacity() {
    for cap in [1, 8, 64] {
        let (tx, rx) = mpsc::sync_channel(cap);
        for i in 0..cap {
            tx.try_send(i).unwrap();
        }
        assert!(matches!(tx.try_send(cap),Err(mpsc::TrySendError::Full(n)) if n==cap));
        for i in 0..cap {
            assert_eq!(rx.try_recv().unwrap(), i);
        }
        assert!(matches!(rx.try_recv(), Err(mpsc::TryRecvError::Empty)));
        drop(rx);
        assert!(matches!(
            tx.try_send(99),
            Err(mpsc::TrySendError::Disconnected(99))
        ));
    }
}
fn fifo() {
    let (tx, rx) = mpsc::sync_channel(8);
    let mut workers = Vec::new();
    for p in 0..4 {
        let tx = tx.clone();
        workers.push(thread::spawn(move || {
            for n in 0..2000 {
                tx.send((p, n)).unwrap();
            }
        }));
    }
    drop(tx);
    let mut next = [0; 4];
    for (p, n) in rx {
        assert_eq!(n, next[p]);
        next[p] += 1;
    }
    for t in workers {
        t.join().unwrap();
    }
    assert_eq!(next, [2000; 4]);
}
fn mpmc_ownership() {
    let drops = Arc::new(AtomicUsize::new(0));
    let seen = Arc::new(Mutex::new(Vec::new()));
    let (tx, rx) = mpmc::sync_channel::<Message>(8);
    let mut readers = Vec::new();
    for _ in 0..2 {
        let rx = rx.clone();
        let seen = seen.clone();
        readers.push(thread::spawn(move || {
            while let Ok(m) = rx.recv() {
                seen.lock().unwrap().push(m.id);
            }
        }));
    }
    drop(rx);
    let mut writers = Vec::new();
    for p in 0..4 {
        let tx = tx.clone();
        let drops = drops.clone();
        writers.push(thread::spawn(move || {
            for n in 0..2000 {
                tx.send(Message {
                    id: p * 2000 + n,
                    drops: drops.clone(),
                })
                .unwrap();
            }
        }));
    }
    drop(tx);
    for t in writers {
        t.join().unwrap();
    }
    for t in readers {
        t.join().unwrap();
    }
    let mut seen = seen.lock().unwrap();
    seen.sort_unstable();
    assert_eq!(*seen, (0..8000).collect::<Vec<_>>());
    assert_eq!(drops.load(SeqCst), 8000);
}
fn disconnect() {
    let drops = Arc::new(AtomicUsize::new(0));
    let (tx, rx) = mpsc::sync_channel(1);
    tx.send(Message {
        id: 0,
        drops: drops.clone(),
    })
    .unwrap();
    let start = Arc::new(Barrier::new(5));
    let mut workers = Vec::new();
    for id in 1..=4 {
        let tx = tx.clone();
        let start = start.clone();
        let drops = drops.clone();
        workers.push(thread::spawn(move || {
            start.wait();
            let e = tx.send(Message { id, drops }).unwrap_err();
            assert_eq!(e.0.id, id);
        }));
    }
    start.wait();
    for _ in 0..10 {
        thread::yield_now();
    }
    drop(rx);
    for t in workers {
        t.join().unwrap();
    }
    drop(tx);
    assert_eq!(drops.load(SeqCst), 5);
}
fn timeouts() {
    for i in 0..300 {
        let drops = Arc::new(AtomicUsize::new(0));
        let (tx, rx) = mpmc::sync_channel(1);
        tx.send(Message {
            id: 0,
            drops: drops.clone(),
        })
        .unwrap();
        let d = drops.clone();
        let sender = thread::spawn(move || {
            tx.send_timeout(Message { id: 1, drops: d }, Duration::from_millis(1))
        });
        match i % 3 {
            0 => {
                assert!(matches!(
                    sender.join().unwrap(),
                    Err(mpmc::SendTimeoutError::Timeout(_))
                ));
                drop(rx);
            }
            1 => {
                assert_eq!(rx.recv().unwrap().id, 0);
                assert_eq!(rx.recv_timeout(Duration::from_secs(1)).unwrap().id, 1);
                assert!(sender.join().unwrap().is_ok());
                drop(rx);
            }
            _ => {
                drop(rx);
                assert!(sender.join().unwrap().is_err());
            }
        }
        assert_eq!(drops.load(SeqCst), 2);
        let (tx, rx) = mpmc::sync_channel(1);
        let d = drops.clone();
        let receiver = thread::spawn(move || rx.recv_timeout(Duration::from_nanos(1)));
        let sent = tx.send_timeout(Message { id: 2, drops: d }, Duration::from_millis(1));
        drop(sent);
        drop(tx);
        drop(receiver.join().unwrap());
        assert_eq!(drops.load(SeqCst), 3);
    }
}
fn other_flavors() {
    let (tx, rx) = mpsc::sync_channel(0);
    let t = thread::spawn(move || tx.send(42).unwrap());
    assert_eq!(rx.recv().unwrap(), 42);
    t.join().unwrap();
    let (tx, rx) = mpsc::channel();
    for n in 0..1000 {
        tx.send(n).unwrap();
    }
    drop(tx);
    assert_eq!(rx.into_iter().sum::<usize>(), 999 * 1000 / 2);
}
fn reentrant_drop() {
    struct Reentrant {
        tx: mpsc::SyncSender<Option<Box<Reentrant>>>,
        drops: Arc<AtomicUsize>,
    }
    impl Drop for Reentrant {
        fn drop(&mut self) {
            assert!(matches!(
                self.tx.try_send(None),
                Err(mpsc::TrySendError::Disconnected(None))
            ));
            self.drops.fetch_add(1, SeqCst);
        }
    }
    let drops = Arc::new(AtomicUsize::new(0));
    let (tx, rx) = mpsc::sync_channel(8);
    for _ in 0..8 {
        tx.send(Some(Box::new(Reentrant {
            tx: tx.clone(),
            drops: drops.clone(),
        })))
        .unwrap();
    }
    drop(tx);
    drop(rx);
    assert_eq!(drops.load(SeqCst), 8);
}
fn preexisting_unpark() {
    let (_tx, rx) = mpsc::sync_channel::<usize>(1);
    thread::current().unpark();
    assert!(matches!(
        rx.recv_timeout(Duration::from_millis(2)),
        Err(mpsc::RecvTimeoutError::Timeout)
    ));
}
thread_local! { static WITNESS_ROLE: std::cell::Cell<u8> = const { std::cell::Cell::new(0) }; }
static WITNESS_ACTIVE: AtomicUsize = AtomicUsize::new(0);
static OWNER_UNLOCKS: AtomicUsize = AtomicUsize::new(0);
static OWNER_REGISTERED: AtomicUsize = AtomicUsize::new(0);
static MATCHER_UNLOCKED: AtomicUsize = AtomicUsize::new(0);
static RELEASE_MATCHER: AtomicUsize = AtomicUsize::new(0);
fn await_flag(flag: &AtomicUsize) {
    let until = std::time::Instant::now() + Duration::from_secs(5);
    while flag.load(SeqCst) == 0 {
        if std::time::Instant::now() >= until {
            std::process::abort();
        }
        thread::yield_now();
    }
}
fn after_witness_unlock() {
    if WITNESS_ACTIVE.load(SeqCst) == 0 {
        return;
    }
    WITNESS_ROLE.with(|role| match role.get() {
        1 if OWNER_UNLOCKS.fetch_add(1, SeqCst) == 1 => {
            OWNER_REGISTERED.store(1, SeqCst);
            await_flag(&MATCHER_UNLOCKED);
        }
        2 => {
            // The matcher published and unlocked, but has not posted yet.
            MATCHER_UNLOCKED.store(1, SeqCst);
            await_flag(&RELEASE_MATCHER);
        }
        _ => {}
    });
}
fn late_notification_after_owner_exit() {
    WITNESS_ACTIVE.store(1, SeqCst);
    let (tx, rx) = mpsc::sync_channel(1);
    let owner = thread::spawn(move || {
        WITNESS_ROLE.with(|role| role.set(1));
        // An unrelated permit lets the owner observe committed output before
        // the delayed channel notification, then retire its TLS/stack entirely.
        thread::current().unpark();
        assert_eq!(rx.recv().unwrap(), 42);
    });
    await_flag(&OWNER_REGISTERED);
    let matcher = thread::spawn(move || {
        WITNESS_ROLE.with(|role| role.set(2));
        tx.send(42).unwrap();
    });
    owner.join().unwrap();
    RELEASE_MATCHER.store(1, SeqCst);
    matcher.join().unwrap();
    WITNESS_ACTIVE.store(0, SeqCst);
}
fn main() {
    fn sender_traits<T: Send + Sync + std::panic::UnwindSafe + std::panic::RefUnwindSafe>() {}
    fn sender_unpin<T: Unpin>() {}
    sender_traits::<mpsc::SyncSender<std::cell::Cell<usize>>>();
    sender_unpin::<mpsc::SyncSender<std::marker::PhantomPinned>>();
    late_notification_after_owner_exit();
    println!("PASS delayed notification after joined owner exit");
    capacity();
    println!("PASS capacity/try/disconnect");
    fifo();
    println!("PASS four-producer FIFO");
    mpmc_ownership();
    println!("PASS MPMC ownership");
    disconnect();
    println!("PASS concurrent disconnect/drop");
    timeouts();
    println!("PASS timeout/transfer ownership races");
    reentrant_drop();
    println!("PASS reentrant buffered destruction outside lock");
    preexisting_unpark();
    println!("PASS preexisting park permit does not complete a channel wait");
    other_flavors();
    println!("PASS zero/unbounded compatibility");
}

// The host fixture supplies real mutual exclusion using the same opaque slot.
// It cannot qualify target interrupt masking or native configuration choices.
#[no_mangle]
pub unsafe extern "C" fn __rust_nuttx_spin_init(storage: *mut usize) {
    unsafe { storage.write(0) };
}
#[no_mangle]
pub unsafe extern "C" fn __rust_nuttx_spin_lock(storage: *mut usize) -> usize {
    let lock = unsafe { AtomicUsize::from_ptr(storage) };
    while lock
        .compare_exchange_weak(
            0,
            1,
            std::sync::atomic::Ordering::Acquire,
            std::sync::atomic::Ordering::Relaxed,
        )
        .is_err()
    {
        std::hint::spin_loop();
    }
    0
}
#[no_mangle]
pub unsafe extern "C" fn __rust_nuttx_spin_unlock(storage: *mut usize, _: usize) {
    unsafe { AtomicUsize::from_ptr(storage) }.store(0, std::sync::atomic::Ordering::Release);
    after_witness_unlock();
}
