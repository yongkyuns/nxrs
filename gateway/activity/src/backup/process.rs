//! Private bounded restic process boundary. Credentials use an anonymous stdin pipe.
use super::{hex, Config, Error};
use std::process::Stdio;
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::{Child, Command},
    sync::watch,
    time::{timeout, timeout_at, Duration, Instant},
};
use zeroize::Zeroizing;

pub(super) struct Restic<'a> {
    pub config: &'a Config,
    pub key: &'a [u8],
    pub deadline: Instant,
    pub stop: watch::Receiver<bool>,
}
async fn capture(input: impl AsyncRead + Unpin, limit: u64) -> Result<Zeroizing<Vec<u8>>, Error> {
    let mut bytes = Zeroizing::new(Vec::new());
    input
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .await
        .map_err(|_| Error::Io)?;
    if bytes.len() as u64 > limit {
        return Err(Error::Output);
    }
    Ok(bytes)
}
async fn retire(child: &mut Child) {
    let mut reported = false;
    loop {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        let _ = child.start_kill();
        if matches!(
            timeout(Duration::from_secs(3), child.wait()).await,
            Ok(Ok(_))
        ) {
            return;
        }
        // Keep the volume lease while cleanup is unproven; never release it and
        // admit another writer merely because an observation deadline expired.
        if !reported {
            eprintln!("backup cleanup pending; keep the service group stopped");
            reported = true;
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}
impl Restic<'_> {
    pub(super) async fn command(&self, args: &[&str]) -> Result<Zeroizing<Vec<u8>>, Error> {
        self.bounded(args, 65536).await
    }
    pub(super) async fn bounded(
        &self,
        args: &[&str],
        limit: u64,
    ) -> Result<Zeroizing<Vec<u8>>, Error> {
        if *self.stop.borrow() {
            return Err(Error::Cancelled);
        }
        if Instant::now() >= self.deadline {
            return Err(Error::Timeout);
        }
        let mut child = Command::new(&self.config.restic)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("LANG", "C")
            .env("GOMAXPROCS", "2")
            .env("TMPDIR", self.config.maintenance().join("temporary"))
            .current_dir(self.config.volume().mount.as_path())
            .args(["--no-cache", "--quiet", "--json", "--repo"])
            .arg(format!("local:{}", self.config.repository.display()))
            .args(["--password-file", "/dev/stdin", "--retry-lock", "0s"])
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| Error::Io)?;
        let handles = (child.stdin.take(), child.stdout.take(), child.stderr.take());
        let answer = {
            let result = async {
                let (Some(mut input), Some(output), Some(errors)) = handles else {
                    return Err(Error::Io);
                };
                timeout(Duration::from_secs(1), input.write_all(self.key))
                    .await
                    .map_err(|_| Error::Timeout)?
                    .map_err(|_| Error::Io)?;
                drop(input);
                let (status, out, _errors) = tokio::try_join!(
                    async { child.wait().await.map_err(|_| Error::Io) },
                    capture(output, limit),
                    capture(errors, 65536)
                )?;
                match status.code() {
                    Some(0) => Ok(out),
                    Some(12) => Err(Error::Authentication),
                    Some(11) => Err(Error::Busy),
                    _ => Err(Error::Native),
                }
            };
            tokio::pin!(result);
            let mut stop = self.stop.clone();
            tokio::select! {
                biased;
                _=stop.changed()=>Err(Error::Cancelled),
                result=timeout_at(self.deadline,&mut result)=>result.map_err(|_|Error::Timeout).and_then(|v|v),
            }
        };
        // The pinned future's borrow ends before retirement.
        retire(&mut child).await;
        answer
    }
    pub(super) async fn repository(&self, expected: Option<&str>) -> Result<String, Error> {
        let bytes = self.command(&["cat", "config"]).await?;
        let value: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| Error::Native)?;
        let id = value["id"]
            .as_str()
            .filter(|id| hex(id, 64))
            .ok_or(Error::Native)?;
        if value["version"] != 2 || expected.is_some_and(|known| known != id) {
            return Err(Error::Repository);
        }
        Ok(id.into())
    }
    pub(super) async fn check(&self) -> Result<(), Error> {
        self.command(&["check", "--read-data"]).await?;
        Ok(())
    }
}
