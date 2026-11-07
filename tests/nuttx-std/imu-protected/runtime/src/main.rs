use acquisition::{Wake, Worker};
use std::process::ExitCode;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    mpsc::{self, SyncSender, TrySendError},
};
use std::time::{Duration, Instant};

const CYCLES: usize = 8;
const DEADLINE: Duration = Duration::from_secs(5);
static TLS_DROPS: AtomicUsize = AtomicUsize::new(0);

thread_local! {
    static TLS_PROBE: TlsProbe = TlsProbe;
}

struct TlsProbe;

impl Drop for TlsProbe {
    fn drop(&mut self) {
        TLS_DROPS.fetch_add(1, Ordering::SeqCst);
    }
}

struct TaskWake(SyncSender<()>);

impl Wake for TaskWake {
    fn wake(&self) -> std::io::Result<()> {
        match self.0.try_send(()) {
            Ok(()) | Err(TrySendError::Full(())) => Ok(()),
            Err(TrySendError::Disconnected(())) => Err(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "worker exited before wake",
            )),
        }
    }
}

#[derive(Clone, Copy)]
enum RuntimeError {
    WorkerPanic,
    Timeout,
    TlsDestructor,
}

fn failed(_: RuntimeError) {}

fn run_cycle(cycle: usize) -> Result<(), RuntimeError> {
    let (wake_tx, wake_rx) = mpsc::sync_channel(1);
    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    let mut worker = Worker::start(
        "protected-runtime-witness",
        8192,
        TaskWake(wake_tx),
        |_| Ok(()),
        failed,
        move |stop, _emit| {
            TLS_PROBE.with(|_| {});
            ready_tx.send(()).map_err(|_| RuntimeError::WorkerPanic)?;
            wake_rx
                .recv_timeout(DEADLINE)
                .map_err(|_| RuntimeError::Timeout)?;
            if !stop.load(Ordering::Acquire) {
                return Err(RuntimeError::WorkerPanic);
            }
            Ok(())
        },
    )
    .map_err(|_| RuntimeError::WorkerPanic)?;

    ready_rx
        .recv_timeout(DEADLINE)
        .map_err(|_| RuntimeError::Timeout)?;
    worker
        .request_stop()
        .map_err(|_| RuntimeError::WorkerPanic)?;

    let until = Instant::now() + DEADLINE;
    while !worker.is_finished() {
        if Instant::now() >= until {
            return Err(RuntimeError::Timeout);
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    worker.join(RuntimeError::WorkerPanic)?;

    let expected = cycle + 1;
    if TLS_DROPS.load(Ordering::SeqCst) != expected {
        return Err(RuntimeError::TlsDestructor);
    }
    Ok(())
}

fn main() -> ExitCode {
    for cycle in 0..CYCLES {
        if let Err(error) = run_cycle(cycle) {
            let reason = match error {
                RuntimeError::WorkerPanic => "worker",
                RuntimeError::Timeout => "timeout",
                RuntimeError::TlsDestructor => "tls-destructor",
            };
            println!("S8_PROTECTED_RUST_FAIL cycle={cycle} reason={reason}");
            return ExitCode::FAILURE;
        }
    }
    println!("S8_PROTECTED_RUST_PASS cycles={CYCLES} tls_destructors={CYCLES}");
    ExitCode::SUCCESS
}
