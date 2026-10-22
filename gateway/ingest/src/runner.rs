use crate::{config::Config, protocol::{line, Reader, Frame}, Error, Result};
use std::{process::Stdio, time::Duration};
use tokio::{io::{AsyncWriteExt, BufReader}, net::TcpStream, process::{Command, Child},
    sync::watch, time::{Instant, timeout, timeout_at, sleep}};

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

fn encoder(config: &Config, token: &str) -> Command {
    let mut command=Command::new(&config.ffmpeg);
    // No shell, environment-proxy fallback, image looping, or constant-rate frame duplication.
    command.env_clear().env("PATH","/usr/bin:/bin").env("LANG","C")
        .args(["-nostdin","-hide_banner","-loglevel","error","-nostats","-stats_period","0.5",
            "-progress","pipe:1","-threads","2","-f","image2pipe","-c:v","mjpeg",
            "-framerate","30","-probesize","153600","-analyzeduration","0",
            "-use_wallclock_as_timestamps","1","-i","pipe:0","-an","-c:v","libx264",
            "-threads","2","-preset","ultrafast","-tune","zerolatency","-profile:v","baseline",
            "-pix_fmt","yuv420p","-bf","0","-g","30","-fps_mode","passthrough",
            "-b:v","1500k","-maxrate","1500k","-bufsize","1500k",
            "-f","rtsp","-rtsp_transport","tcp","-rw_timeout","5000000"])
        .arg(format!("rtsp://{}:{}@{}/{}",config.publisher_user,token,config.rtsp,config.stream_path))
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true);
    command
}
async fn pump(mut source: Reader<TcpStream>, mut sink: tokio::process::ChildStdin,
              budget: Duration,
    metadata: &watch::Sender<Option<crate::detections::Sample>>,
) -> Result<()> {
    let mut lag=Lag::default();
    loop {
        // One deadline spans the entire next header, JPEG, metadata check and encoder write.
        // Neither a partial TCP byte nor a partial pipe write resets it.
        timeout(budget,async {
            let frame=source.next_frame().await?;
            lag.observe(frame,Instant::now())?;
            sink.write_all(source.payload(frame)).await?;
            if let Some(s) = source.metadata.as_ref() {
                metadata.send_replace(Some(crate::detections::Sample {
                    frame,
                    source: s.clone(),
                    received: Instant::now(),
                }));
            }
            Ok::<(),Error>(())
        }).await.map_err(|_|Error::Timeout)??;
    }
}
async fn encoder_progress(stdout: tokio::process::ChildStdout, budget: Duration) -> Result<()> {
    let mut input=BufReader::with_capacity(4096,stdout);
    let mut deadline=Instant::now()+Duration::from_secs(10);
    let mut count=0u64;
    loop {
        let bytes=timeout_at(deadline,line(&mut input,512)).await.map_err(|_|Error::Encoder)??;
        let text=std::str::from_utf8(&bytes).map_err(|_|Error::Encoder)?;
        if let Some(value)=text.trim().strip_prefix("frame=") {
            let next:u64=value.trim().parse().map_err(|_|Error::Encoder)?;
            if next<count { return Err(Error::Encoder); }
            if next>count { count=next; deadline=Instant::now()+budget; }
        }
    }
}
async fn retire(child: &mut Child) -> Result<()> {
    if child.try_wait().map_err(|_|Error::Cleanup)?.is_some() { return Ok(()); }
    child.start_kill().map_err(|_|Error::Cleanup)?;
    timeout(Duration::from_secs(3),child.wait()).await.map_err(|_|Error::Cleanup)?
        .map_err(|_|Error::Cleanup)?;
    Ok(())
}
async fn attempt(config:&Config, token:&str, stop:&mut watch::Receiver<bool>) -> Result<()> {
    if *stop.borrow() { return Err(Error::Stopped); }
    let source=tokio::select! {
        biased;
        _=stop.changed()=>return Err(Error::Stopped),
        result=timeout(Duration::from_secs(5),async {
            let mut socket=TcpStream::connect(config.camera).await?;
            socket.set_nodelay(true)?;
            let path=if config.detections.is_some() { "/stream?metadata=1" } else { "/stream" };
            let request=format!("GET {path} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n",config.camera);
            socket.write_all(request.as_bytes()).await?;
            Reader::start(socket,config.width,config.height).await
        })=> result.map_err(|_|Error::Timeout)??,
    };
    let mut child=encoder(config,token).spawn().map_err(|_|Error::Encoder)?;
    let (metadata, rx) = watch::channel(None);
    let (end, ended) = watch::channel(false);
    let publisher = config.detections.clone().map(|c| {
        tokio::spawn(crate::detections::run(
            c,
            config.publisher_user.clone(),
            token.to_owned(),
            rx,
            ended,
        ))
    });
    // These handles exist by construction; every post-spawn path still reaps the process.
    let outcome=match (child.stdin.take(),child.stdout.take()) {
        (Some(stdin),Some(stdout))=>{
            let budget=Duration::from_millis(config.frame_timeout_ms);
            tokio::select! {
                biased;
                _=stop.changed()=>Err(Error::Stopped),
                _=child.wait()=>Err(Error::Encoder),
                result=pump(source,stdin,budget,&metadata)=>result,
                result=encoder_progress(stdout,budget.max(Duration::from_secs(3)))=>result,
            }
        }
        _=>Err(Error::Encoder),
    };
    // Source socket and pending JPEG/pipe futures have been dropped before reconnect.
    // Never create another encoder if the previous one cannot be reaped.
    let _ = end.send(true);
    let publisher_failed = if let Some(publisher) = publisher {
        publisher.await.is_err()
    } else {
        false
    };
    retire(&mut child).await?;
    if publisher_failed {
        return Err(Error::Cleanup);
    }
    outcome
}
/// One source connection and encoder at a time. Retry delay caps at eight seconds.
pub async fn run(config:Config, mut stop:watch::Receiver<bool>) -> Result<()> {
    config.validate()?;
    let token=config.token()?;
    let mut delay=1;
    loop {
        let start=Instant::now();
        match attempt(&config,&token,&mut stop).await {
            Err(Error::Stopped)=>return Ok(()),
            Err(Error::Cleanup)=>return Err(Error::Cleanup),
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
            "ffmpeg":"/does/not/exist","width":320,"height":240,"frame_timeout_ms":3000
        })).unwrap();
        let (_tx,mut rx)=watch::channel(true);
        assert_eq!(attempt(&c,"not-used",&mut rx).await,Err(Error::Stopped));
    }
}
