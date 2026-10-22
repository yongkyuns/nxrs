#![forbid(unsafe_code)]
use nxrs_camera_activity::{
    store::Error,
    worker::{self, Config},
};
use std::path::Path;
use tokio::sync::watch;

async fn run() -> Result<(), Error> {
    let mut args = std::env::args().skip(1);
    let path = args.next().ok_or(Error::Invalid)?;
    if args.next().is_some() {
        return Err(Error::Invalid);
    }
    let config = Config::load(Path::new(&path))?;
    let (stop, rx) = watch::channel(false);
    let worker = worker::run(config, rx);
    tokio::pin!(worker);
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .map_err(|_| Error::Io)?;
    tokio::select! {
        result = &mut worker => result,
        _ = async { tokio::select! { _ = term.recv() => {}, _ = tokio::signal::ctrl_c() => {} } } => {
            let _ = stop.send(true); worker.await
        }
    }
}
fn main() {
    // Explicit ownership: one async runtime thread and at most two admitted blocking I/O workers.
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(2)
        .build()
        .expect("runtime");
    if let Err(error) = runtime.block_on(run()) {
        eprintln!("nxrs-camera-activity: {error}");
        std::process::exit(1);
    }
}
