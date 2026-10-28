use crate::{tls::Prepared, config::Config, protocol::{line, Reader, Frame}, Error, Result};
use std::{process::Stdio, time::Duration};
use tokio::{io::{AsyncWriteExt, BufReader}, net::TcpStream, process::{Command, Child},
    sync::watch, time::{Instant, timeout, timeout_at, sleep}};

/// Detect *additional* backlog relative to the best observed offset this connection.
/// No clock synchronization or first-frame/exposure-age claim follows from this measurement.
#[derive(Default)]
pub(crate) struct Lag {
    baseline: Option<(Instant,u64)>,
    best: i128,
}
impl Lag {
    pub(crate) fn observe(&mut self, frame: Frame, now: Instant) -> Result<()> {
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

pub(crate) fn encoder(config: &Config, token: &str, elementary: bool) -> Command {
    let mut command=Command::new(&config.ffmpeg);
    // No shell, environment-proxy fallback, image looping, or constant-rate frame duplication.
    command.env_clear().env("PATH","/usr/bin:/bin").env("LANG","C")
        .args(["-nostdin","-hide_banner","-loglevel","error","-nostats","-stats_period","0.5",
            "-xerror","-err_detect","explode","-progress",if elementary { "pipe:2" } else { "pipe:1" },
            "-threads","2","-f","image2pipe","-c:v","mjpeg",
            "-framerate","30","-probesize",if elementary { "32" } else { "153600" },"-analyzeduration",if elementary { "1" } else { "0" }]);
    if !elementary { command.args(["-use_wallclock_as_timestamps","1"]); }
    command.args(["-i","pipe:0","-an","-c:v","libx264",
            "-threads","2","-preset","ultrafast","-tune","zerolatency","-profile:v","baseline",
            "-pix_fmt","yuv420p","-bf","0","-g","30","-fps_mode","passthrough",
            "-b:v","1500k","-maxrate","1500k","-bufsize","1500k"]);
    if elementary {
        command.args(["-x264-params","aud=1","-flush_packets","1","-f","h264","pipe:1"])
            .stderr(Stdio::piped());
    } else {
        command.args(["-f","rtsp","-rtsp_transport","tcp","-rw_timeout","5000000"])
            .arg(format!("rtsp://{}:{}@{}/{}",config.publisher_user,token,config.rtsp,config.stream_path))
            .stderr(Stdio::null());
    }
    command.stdin(Stdio::piped()).stdout(Stdio::piped()).kill_on_drop(true);
    command
}
async fn pump(mut source: Reader<crate::tls::Socket>, mut sink: tokio::process::ChildStdin,
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
enum ProgressCounter {
    Encoded(u64),
    Published(Option<i64>),
}
impl ProgressCounter {
    fn observe(&mut self, text: &str) -> Result<bool> {
        match self {
            Self::Encoded(count) => {
                let Some(value)=text.strip_prefix("frame=") else { return Ok(false); };
                let next:u64=value.trim().parse().map_err(|_|Error::Encoder)?;
                if next<*count { return Err(Error::Encoder); }
                let advanced=next>*count;
                *count=next;
                Ok(advanced)
            }
            Self::Published(previous) => {
                // FFmpeg 6 emits no frame counter for stream-copy output.
                // A finite muxed output time advances only after packet output;
                // it is a health signal, never an annotation/frame identity.
                let Some(value)=text.strip_prefix("out_time_us=") else { return Ok(false); };
                if value.trim()=="N/A" { return Ok(false); }
                let next:i64=value.trim().parse().map_err(|_|Error::Encoder)?;
                if previous.is_some_and(|last|next<last) { return Err(Error::Encoder); }
                let advanced=previous.map_or(next>0,|last|next>last);
                *previous=Some(next);
                Ok(advanced)
            }
        }
    }
}
async fn media_progress<R: tokio::io::AsyncRead + Unpin>(stdout: R, budget: Duration,
    mut counter: ProgressCounter) -> Result<()> {
    let mut input=BufReader::with_capacity(4096,stdout);
    let mut deadline=Instant::now()+Duration::from_secs(10);
    loop {
        let bytes=timeout_at(deadline,line(&mut input,512)).await.map_err(|_|Error::Encoder)??;
        let text=std::str::from_utf8(&bytes).map_err(|_|Error::Encoder)?;
        if counter.observe(text.trim())? { deadline=Instant::now()+budget; }
    }
}
pub(crate) async fn encoder_progress<R: tokio::io::AsyncRead + Unpin>(stdout: R, budget: Duration) -> Result<()> {
    media_progress(stdout,budget,ProgressCounter::Encoded(0)).await
}
pub(crate) async fn publisher_progress<R: tokio::io::AsyncRead + Unpin>(stdout: R, budget: Duration) -> Result<()> {
    media_progress(stdout,budget,ProgressCounter::Published(None)).await
}
pub(crate) async fn retire(child: &mut Child) -> Result<()> {
    if child.try_wait().map_err(|_|Error::Cleanup)?.is_some() { return Ok(()); }
    child.start_kill().map_err(|_|Error::Cleanup)?;
    timeout(Duration::from_secs(3),child.wait()).await.map_err(|_|Error::Cleanup)?
        .map_err(|_|Error::Cleanup)?;
    Ok(())
}
pub(crate) async fn camera_source(config:&Config, policy:&Prepared, stop:&mut watch::Receiver<bool>) -> Result<Reader<crate::tls::Socket>> {
    if *stop.borrow() { return Err(Error::Stopped); }
    tokio::select! {
        biased;
        _=stop.changed()=>Err(Error::Stopped),
        error=policy.guard()=>Err(error),
        result=timeout(Duration::from_secs(5),async {
            let socket=TcpStream::connect(config.camera).await?;
            socket.set_nodelay(true)?;
            let mut socket=policy.connect(socket).await?;
            let path=if config.detections.is_some() { "/stream?metadata=1" } else { "/stream" };
            let request=format!("GET {path} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n",config.camera);
            socket.write_all(request.as_bytes()).await?;
            Reader::start(socket,config.width,config.height).await
        })=>result.map_err(|_|Error::Timeout)?,
    }
}
async fn attempt(config:&Config, token:&str, policy:&Prepared, stop:&mut watch::Receiver<bool>) -> Result<()> {
    if *stop.borrow() { return Err(Error::Stopped); }
    if config.detections.is_some() { return crate::video::attempt(config, token, policy, stop).await; }
    let source=camera_source(config,policy,stop).await?;
    let mut child=encoder(config,token,false).spawn().map_err(|_|Error::Encoder)?;
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
                error=policy.guard()=>Err(error),
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
            error=policy.guard()=>return Err(error),
            _=sleep(Duration::from_secs(delay))=>{},
        }
        delay=(delay*2).min(8);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn progress_matches_encoded_and_copy_output_contracts() {
        let mut encoded=ProgressCounter::Encoded(0);
        assert_eq!(encoded.observe("out_time_us=40000"),Ok(false));
        assert_eq!(encoded.observe("frame=0"),Ok(false));
        assert_eq!(encoded.observe("frame=2"),Ok(true));
        assert_eq!(encoded.observe("frame=2"),Ok(false));
        assert_eq!(encoded.observe("frame=1"),Err(Error::Encoder));
        let mut copy=ProgressCounter::Published(None);
        assert_eq!(copy.observe("out_time_us=N/A"),Ok(false));
        assert_eq!(copy.observe("out_time_us=-40000"),Ok(false));
        assert_eq!(copy.observe("out_time_us=0"),Ok(true));
        assert_eq!(copy.observe("out_time_us=40000"),Ok(true));
        assert_eq!(copy.observe("frame=1000"),Ok(false));
        assert_eq!(copy.observe("out_time_us=40000"),Ok(false));
        assert_eq!(copy.observe("out_time_us=39999"),Err(Error::Encoder));
        assert_eq!(copy.observe("out_time_us=invalid"),Err(Error::Encoder));
    }
    #[tokio::test] async fn copy_progress_renews_without_an_encoded_frame_counter() {
        let (mut output,input)=tokio::io::duplex(4096);
        let producer=tokio::spawn(async move {
            for timestamp in [0,40000,80000,120000] {
                output.write_all(format!("out_time_us={timestamp}\nprogress=continue\n").as_bytes()).await.unwrap();
                sleep(Duration::from_millis(100)).await;
            }
            std::future::pending::<()>().await;
        });
        let start=Instant::now();
        let outcome=timeout(Duration::from_secs(3),publisher_progress(input,Duration::from_millis(500))).await;
        producer.abort(); let _=producer.await;
        assert_eq!(outcome.unwrap(),Err(Error::Encoder));
        assert!(start.elapsed()>=Duration::from_millis(600));
    }
    #[tokio::test] async fn repeated_copy_time_and_unrelated_fields_do_not_renew_deadline() {
        let (mut output,input)=tokio::io::duplex(4096);
        let producer=tokio::spawn(async move {
            let mut frames=1;
            loop {
                if output.write_all(format!("frame={frames}\nout_time_us=40000\nprogress=continue\n").as_bytes()).await.is_err() { return; }
                frames+=1;
                sleep(Duration::from_millis(25)).await;
            }
        });
        let outcome=timeout(Duration::from_secs(2),publisher_progress(input,Duration::from_millis(250))).await;
        producer.abort(); let _=producer.await;
        assert_eq!(outcome.unwrap(),Err(Error::Encoder));
    }
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
        assert_eq!(attempt(&c,"not-used",&Prepared::load(&c).unwrap(),&mut rx).await,Err(Error::Stopped));
    }
}
