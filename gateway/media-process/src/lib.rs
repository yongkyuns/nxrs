//! Owned child supervision used by gateway services on their existing runtime.
//! The isolation launcher itself remains synchronous and does not use this crate.
#![forbid(unsafe_code)]
use std::{path::Path, process::Stdio, time::Duration};
use tokio::{
    io::AsyncReadExt,
    process::{Child, Command},
    time::timeout,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unavailable,
    Cleanup,
}

/// Confirm exit/reaping before another generation can own resources.
pub async fn retire(child: &mut Child) -> Result<(), Error> {
    if child.try_wait().map_err(|_| Error::Cleanup)?.is_some() {
        return Ok(());
    }
    child.start_kill().map_err(|_| Error::Cleanup)?;
    timeout(Duration::from_secs(3), child.wait())
        .await
        .map_err(|_| Error::Cleanup)?
        .map_err(|_| Error::Cleanup)?;
    Ok(())
}

/// Probe the exact installed launcher before admitting credentials or children.
/// Output, elapsed time, inherited environment and lifetime are all bounded.
pub async fn probe(launcher: &Path) -> Result<(), Error> {
    let mut child = Command::new(launcher)
        .arg("--check")
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LANG", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| Error::Unavailable)?;
    let outcome = timeout(Duration::from_secs(3), async {
        let stdout = child.stdout.take().ok_or(Error::Unavailable)?;
        let mut bytes = Vec::new();
        let mut bounded = stdout.take(129);
        let (read, exit) = tokio::join!(bounded.read_to_end(&mut bytes), child.wait());
        if read.is_err()
            || !exit.map_err(|_| Error::Unavailable)?.success()
            || bytes != b"NXRS_MEDIA_ISOLATION_READY\n"
        {
            return Err(Error::Unavailable);
        }
        Ok(())
    })
    .await
    .map_err(|_| Error::Unavailable)
    .and_then(|value| value);
    retire(&mut child).await?;
    outcome
}
