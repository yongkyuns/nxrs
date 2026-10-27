#![forbid(unsafe_code)]
use nxrs_camera_activity::backup::{self, Error, Operation};
use std::path::Path;
use tokio::sync::watch;
async fn run() -> Result<(), Error> {
    let mut arguments = std::env::args().skip(1);
    let config = arguments.next().ok_or(Error::Invalid)?;
    let operation = match arguments.next().as_deref() {
        Some("init") => Operation::Init,
        Some("backup") => Operation::Backup,
        Some("check") => Operation::Check,
        Some("unlock-stale") => Operation::UnlockStale,
        Some("restore") => Operation::Restore(arguments.next().ok_or(Error::Invalid)?),
        Some("resume") => Operation::Resume(arguments.next().ok_or(Error::Invalid)?),
        _ => return Err(Error::Invalid),
    };
    if arguments.next().is_some() {
        return Err(Error::Invalid);
    }
    let (stop, rx) = watch::channel(false);
    let operation = backup::execute(Path::new(&config), operation, rx);
    tokio::pin!(operation);
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .map_err(|_| Error::Io)?;
    let receipt = tokio::select! {
        result=&mut operation=>result?,
        _=async {tokio::select! {_=tokio::signal::ctrl_c()=>{},_=term.recv()=>{}}}=>{
            let _=stop.send(true);operation.await?
        },
    };
    println!(
        "{}",
        serde_json::json!({"repository_id":receipt.repository_id,"snapshot_id":receipt.snapshot_id})
    );
    Ok(())
}
fn main() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(2)
        .build()
        .expect("runtime");
    if let Err(error) = runtime.block_on(run()) {
        eprintln!("nxrs-activity-backup: {error}");
        std::process::exit(1);
    }
}
