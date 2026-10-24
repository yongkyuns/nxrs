use crate::{tls::Prepared, config::Config, protocol::{line, Reader, Frame}, Error, Result};
use std::{process::Stdio, time::Duration};
use tokio::{io::{AsyncReadExt, AsyncWriteExt, BufReader}, net::TcpStream, process::{Command, Child},
    sync::watch, time::{Instant, timeout, timeout_at, sleep}};

const STARTUP_OUTPUT:Duration = Duration::from_secs(10);
/// Probe the same installed helper before loading source/encoder resources.
/// This owns a disposable child; failed enforcement never falls back.
pub async fn check_encoder_isolation(launcher: &std::path::Path) -> Result<()> {
    let mut child = Command::new(launcher).arg("--check").env_clear()
        .env("PATH", "/usr/bin:/bin").env("LANG", "C")
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null())
        .kill_on_drop(true).spawn().map_err(|_| Error::Configuration)?;
    let stdout = child.stdout.take().ok_or(Error::Configuration)?;
    let outcome = timeout(Duration::from_secs(3), async {
        let mut bytes = Vec::new();
        let mut bounded = stdout.take(129);
        let (read, exit) = tokio::join!(bounded.read_to_end(&mut bytes), child.wait());
        if read.is_err() || !exit.map_err(|_| Error::Configuration)?.success()
            || bytes != b"NXRS_MEDIA_ISOLATION_READY\n" { return Err(Error::Configuration); }
        Ok(())
    }).await.map_err(|_| Error::Configuration).and_then(|value| value);
    retire(&mut child).await?;
    outcome
}


/// Fixed local operation names only; never log transport errors, paths or peer bytes.
#[derive(Clone, Copy, Debug)]
enum Stage { CameraSetup, CameraFrames, EncodedRelay, EncoderProgress, PublisherProgress }
async fn observed<T>(stage:Stage, operation:impl std::future::Future<Output=Result<T>>) -> Result<T> {
    operation.await.inspect_err(|error| {
        eprintln!("nxrs-camera-ingest: stage-failure ({stage:?}: {error})");
    })
}

/// Detect *additional* backlog relative to the best observed offset this connection.
/// No clock synchronization or first-frame/exposure-age claim follows from this measurement.
#[derive(Default)]
struct Lag {
    baseline: Option<(Instant,u64)>,
    best: i128,
}
impl Lag {
    fn observe(&mut self, frame: Frame, now: Instant) -> Result<()> {
        let Some((start,device))=self.baseline else {
            self.baseline=Some((now,frame.capture_ms)); return Ok(());
        };
        let elapsed=now.duration_since(start).as_millis() as i128;
        let delta=elapsed-(i128::from(frame.capture_ms)-i128::from(device));
        self.best=self.best.min(delta);
        if delta-self.best>1000 { return Err(Error::Stale); }
        Ok(())
    }
}

fn encoder(config: &Config) -> Command {
    let mut command = match &config.media_sandbox {
        Some(launcher) => Command::from(nxrs_media_sandbox::command(launcher, &config.ffmpeg, None)),
        None => Command::new(&config.ffmpeg),
    };
    // No shell, environment-proxy fallback, image looping, or constant-rate frame duplication.
    command.env_clear().env("PATH","/usr/bin:/bin").env("LANG","C")
        .args(["-nostdin","-hide_banner","-loglevel","quiet","-nostats","-stats_period","0.5",
            "-progress","pipe:2","-threads","2","-f","image2pipe","-c:v","mjpeg",
            "-framerate","30","-probesize","153600","-analyzeduration","0",
            "-use_wallclock_as_timestamps","1","-i","pipe:0","-an","-c:v","libx264",
            "-threads","2","-preset","ultrafast","-tune","zerolatency","-profile:v","baseline",
            "-pix_fmt","yuv420p","-bf","0","-g","30","-fps_mode","passthrough",
            "-b:v","1500k","-maxrate","1500k","-bufsize","1500k",
            "-f","mpegts","-muxdelay","0","-flush_packets","1","pipe:1"])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(true);
    command
}
async fn pump<R: tokio::io::AsyncRead + Unpin>(mut source: Reader<R>, mut sink: tokio::process::ChildStdin,
              budget: Duration) -> Result<()> {
    let mut lag=Lag::default();
    loop {
        // One deadline spans the entire next header, JPEG, metadata check and encoder write.
        // Neither a partial TCP byte nor a partial pipe write resets it.
        timeout(budget,async {
            let frame=source.next_frame().await?;
            lag.observe(frame,Instant::now())?;
            sink.write_all(source.payload(frame)).await?;
            Ok::<(),Error>(())
        }).await.map_err(|_|Error::Timeout)??;
    }
}
async fn publication_progress<R: tokio::io::AsyncRead+Unpin>(stdout: R, prefix: &str, budget: Duration) -> Result<()> {
    let mut input=BufReader::with_capacity(4096,stdout);
    let mut deadline=Instant::now()+STARTUP_OUTPUT;
    let mut count=0u64;
    loop {
        let bytes=timeout_at(deadline,line(&mut input,512)).await.map_err(|_|Error::Encoder)??;
        let text=std::str::from_utf8(&bytes).map_err(|_|Error::Encoder)?;
        if let Some(value)=text.trim().strip_prefix(prefix) {
            let next:u64=value.trim().parse().map_err(|_|Error::Encoder)?;
            if next<count { return Err(Error::Encoder); }
            if next>count { count=next; deadline=Instant::now()+budget; }
        }
    }
}
fn publisher(config: &Config) -> Command {
    let mut command=Command::new(&config.publisher);
    command.env_clear().env("PATH","/usr/bin:/bin").env("LANG","C")
        .args([config.rtsp.to_string(),config.publisher_user.clone(),config.stream_path.clone()])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true);
    command
}
async fn relay(mut input:tokio::process::ChildStdout, mut output:tokio::process::ChildStdin,
               token:&str, budget:Duration) -> Result<()> {
    // Only the publisher receives the fixed-size credential preamble. The image
    // encoder receives JPEGs and emits MPEG-TS; its argv/environment contain no token.
    timeout(budget,output.write_all(token.as_bytes())).await.map_err(|_|Error::Timeout)??;
    let mut bytes=[0u8;4096];
    let mut first=true;
    loop {
        // Match both existing progress watchdogs while the encoder probes its
        // initial input. Every later transfer keeps the strict steady budget.
        let limit=if first {STARTUP_OUTPUT} else {budget};
        timeout(limit,async {
            let count=input.read(&mut bytes).await?;
            if count==0 { return Err(Error::Encoder); }
            output.write_all(&bytes[..count]).await?;
            Ok::<(),Error>(())
        }).await.map_err(|_|Error::Timeout)??;
        first=false;
    }
}
async fn retire(child: &mut Child) -> Result<()> {
    if child.try_wait().map_err(|_|Error::Cleanup)?.is_some() { return Ok(()); }
    child.start_kill().map_err(|_|Error::Cleanup)?;
    timeout(Duration::from_secs(3),child.wait()).await.map_err(|_|Error::Cleanup)?
        .map_err(|_|Error::Cleanup)?;
    Ok(())
}
async fn attempt(config:&Config, token:&str, policy:&Prepared, stop:&mut watch::Receiver<bool>) -> Result<()> {
    if *stop.borrow() { return Err(Error::Stopped); }
    let source=tokio::select! {
        biased;
        _=stop.changed()=>return Err(Error::Stopped),
        error=policy.guard()=>return Err(error),
        result=observed(Stage::CameraSetup,async {timeout(Duration::from_secs(5),async {
            let socket=TcpStream::connect(config.camera).await?;
            socket.set_nodelay(true)?;
            let mut socket=policy.connect(socket).await?;
            let request=format!("GET /stream HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n",config.camera);
            socket.write_all(request.as_bytes()).await?;
            Reader::start(socket,config.width,config.height).await
        }).await.map_err(|_|Error::Timeout)?})=> result?,
    };
    let mut publication=publisher(config).spawn().map_err(|_|Error::Encoder)?;
    let mut child=match encoder(config).spawn() {
        Ok(child)=>child,
        Err(_)=>{ retire(&mut publication).await?; return Err(Error::Encoder); },
    };
    // Every post-spawn path retires BOTH processes, including partial setup and
    // a failed reap. Admission never overlaps the previous encoder/publication.
    let outcome=match (child.stdin.take(),child.stdout.take(),child.stderr.take(),
                       publication.stdin.take(),publication.stdout.take()) {
        (Some(stdin),Some(encoded),Some(progress),Some(publish),Some(published))=>{
            let budget=Duration::from_millis(config.frame_timeout_ms);
            tokio::select! {
                biased;
                _=stop.changed()=>Err(Error::Stopped),
                _=child.wait()=>Err(Error::Encoder),
                _=publication.wait()=>Err(Error::Encoder),
                error=policy.guard()=>Err(error),
                result=observed(Stage::CameraFrames,pump(source,stdin,budget))=>result,
                result=observed(Stage::EncodedRelay,relay(encoded,publish,token,budget))=>result,
                result=observed(Stage::EncoderProgress,publication_progress(progress,"frame=",budget.max(Duration::from_secs(3))))=>result,
                result=observed(Stage::PublisherProgress,publication_progress(published,"packet=",budget.max(Duration::from_secs(3))))=>result,
            }
        },
        _=>Err(Error::Encoder),
    };
    let (encoder_retired,publisher_retired)=tokio::join!(retire(&mut child),retire(&mut publication));
    encoder_retired?;
    publisher_retired?;
    outcome
}
/// One source connection and encoder at a time. Retry delay caps at eight seconds.
pub async fn run(config:Config, mut stop:watch::Receiver<bool>) -> Result<()> {
    config.validate()?;
    let token=zeroize::Zeroizing::new(config.token()?);
    let policy=Prepared::load(&config)?;
    let mut delay=1;
    loop {
        let start=Instant::now();
        match attempt(&config,&token,&policy,&mut stop).await {
            Err(Error::Stopped)=>return Ok(()),
            Err(Error::Cleanup)=>return Err(Error::Cleanup),
            Err(Error::Authentication)=>return Err(Error::Authentication),
            result=>{
                let category=result.err().unwrap_or(Error::Encoder);
                eprintln!("nxrs-camera-ingest: retired source/encoder ({category}); retrying");
            }
        }
        if start.elapsed()>=Duration::from_secs(30) { delay=1; }
        tokio::select! {
            biased;
            _=stop.changed()=>return Ok(()),
            _=sleep(Duration::from_secs(delay))=>{},
        }
        delay=(delay*2).min(8);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn arrival_clock_is_relative_not_device_epoch() {
        let mut lag=Lag::default(); let t=Instant::now();
        let f=Frame{length:4,sequence:1,capture_ms:9_000_000};
        assert!(lag.observe(f,t).is_ok());
        assert!(lag.observe(Frame{capture_ms:f.capture_ms+100,..f},t+Duration::from_millis(100)).is_ok());
        assert_eq!(lag.observe(Frame{capture_ms:f.capture_ms+150,..f},t+Duration::from_millis(1200)),Err(Error::Stale));
    }
    #[tokio::test] async fn already_cancelled_does_not_connect_or_spawn() {
        let c:Config=serde_json::from_value(serde_json::json!({
            "camera":"127.0.0.1:1","allow_insecure_camera_http":true,"rtsp":"127.0.0.1:2",
            "stream_path":"reference","publisher_user":"publisher_reference","publisher_token_file":"/private/token",
            "ffmpeg":"/does/not/exist","publisher":"/does/not/exist-either","width":320,"height":240,"frame_timeout_ms":3000
        })).unwrap();
        let (_tx,mut rx)=watch::channel(true);
        assert_eq!(attempt(&c,"not-used",&Prepared::load(&c).unwrap(),&mut rx).await,Err(Error::Stopped));
    }
}
