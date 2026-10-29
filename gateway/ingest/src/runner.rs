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

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum EncodedMedia { MpegTs, AnnexB }

pub(crate) fn encoder(config: &Config, format: EncodedMedia) -> Command {
    let elementary = format == EncodedMedia::AnnexB;
    let mut command=Command::new(&config.ffmpeg);
    // No shell, environment-proxy fallback, image looping, or constant-rate frame duplication.
    command.env_clear().env("PATH","/usr/bin:/bin").env("LANG","C")
        .args(["-nostdin","-hide_banner","-loglevel","quiet","-nostats","-stats_period","0.5",
            "-xerror","-err_detect","explode","-progress","pipe:2",
            "-threads","2","-f","image2pipe","-c:v","mjpeg",
            "-framerate","30","-probesize",if elementary { "32" } else { "153600" },"-analyzeduration",if elementary { "1" } else { "0" }]);
    if !elementary { command.args(["-use_wallclock_as_timestamps","1"]); }
    command.args(["-i","pipe:0","-an","-c:v","libx264",
            "-threads","2","-preset","ultrafast","-tune","zerolatency","-profile:v","baseline",
            "-pix_fmt","yuv420p","-bf","0","-g","30","-fps_mode","passthrough",
            "-b:v","1500k","-maxrate","1500k","-bufsize","1500k"]);
    match format {
        EncodedMedia::MpegTs => { command.args(["-f","mpegts","-muxdelay","0"]); }
        EncodedMedia::AnnexB => { command.args(["-x264-params","aud=1","-f","h264"]); }
    }
    command.args(["-flush_packets","1","pipe:1"])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(true);
    command
}
pub(crate) fn publisher(config: &Config, format: EncodedMedia) -> Command {
    let mut command=Command::new(&config.publisher);
    command.env_clear().env("PATH","/usr/bin:/bin").env("LANG","C")
        .args([config.rtsp.to_string(),config.publisher_user.clone(),config.stream_path.clone()]);
    if format==EncodedMedia::AnnexB { command.arg("annexb"); }
    // The publisher suppresses SDK logs and emits only fixed failure categories.
    command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit()).kill_on_drop(true);
    command
}
/// Only the publication pipe gets the credential; the encoder receives JPEGs only.
pub(crate) async fn preamble(output: &mut tokio::process::ChildStdin, token: &str) -> Result<()> {
    timeout(Duration::from_secs(3),output.write_all(token.as_bytes())).await.map_err(|_|Error::Encoder)??;
    Ok(())
}
async fn relay<R: tokio::io::AsyncRead + Unpin>(mut encoded:R, mut output:tokio::process::ChildStdin,
    token:&str, budget:Duration) -> Result<()> {
    use tokio::io::AsyncReadExt;
    preamble(&mut output,token).await?;
    let mut bytes=[0;4096];
    let mut deadline=Instant::now()+Duration::from_secs(10);
    loop {
        timeout_at(deadline,async {
            let n=encoded.read(&mut bytes).await?;
            if n==0 { return Err(Error::Encoder); }
            output.write_all(&bytes[..n]).await?;
            Ok::<(),Error>(())
        }).await.map_err(|_|Error::Encoder)??;
        deadline=Instant::now()+budget;
    }
}
async fn pump(mut source: Reader<crate::tls::Socket>, mut sink: tokio::process::ChildStdin,
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
enum ProgressCounter { Encoded(u64), Published(u64) }
impl ProgressCounter {
    fn observe(&mut self, text: &str) -> Result<bool> {
        let (prefix,count)=match self {
            Self::Encoded(count)=>("frame=",count),
            Self::Published(count)=>("packet=",count),
        };
        let Some(value)=text.strip_prefix(prefix) else { return Ok(false); };
        let next:u64=value.trim().parse().map_err(|_|Error::Encoder)?;
        if next<*count { return Err(Error::Encoder); }
        let advanced=next>*count;
        *count=next;
        Ok(advanced)
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
    media_progress(stdout,budget,ProgressCounter::Published(0)).await
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
    let mut encoder=encoder(config,EncodedMedia::MpegTs).spawn().map_err(|_|Error::Encoder)?;
    let mut publisher=match publisher(config,EncodedMedia::MpegTs).spawn() {
        Ok(child)=>child,
        Err(_)=>{ retire(&mut encoder).await?; return Err(Error::Encoder); }
    };
    let outcome=match (encoder.stdin.take(),encoder.stdout.take(),encoder.stderr.take(),publisher.stdin.take(),publisher.stdout.take()) {
        (Some(input),Some(encoded),Some(progress),Some(output),Some(published))=>{
            let budget=Duration::from_millis(config.frame_timeout_ms);
            tokio::select! {
                biased;
                _=stop.changed()=>Err(Error::Stopped),
                status=encoder.wait()=>{
                    eprintln!("nxrs-camera-ingest: encoder exit {:?}",status.ok().and_then(|s|s.code()));
                    Err(Error::Encoder)
                },
                status=publisher.wait()=>{
                    eprintln!("nxrs-camera-ingest: publisher exit {:?}",status.ok().and_then(|s|s.code()));
                    Err(Error::Encoder)
                },
                error=policy.guard()=>Err(error),
                result=pump(source,input,budget)=>result,
                result=relay(encoded,output,token,budget.max(Duration::from_secs(5)))=>{ eprintln!("nxrs-camera-ingest: encoded relay stopped"); result },
                result=encoder_progress(progress,budget.max(Duration::from_secs(3)))=>{ eprintln!("nxrs-camera-ingest: encoder progress stopped"); result },
                result=publisher_progress(published,budget.max(Duration::from_secs(3)))=>{ eprintln!("nxrs-camera-ingest: publisher progress stopped"); result },
            }
        }
        _=>Err(Error::Encoder),
    };
    // Attempt both barriers. A failed reap forbids another source admission.
    let a=retire(&mut encoder).await;
    let b=retire(&mut publisher).await;
    if a.is_err() || b.is_err() { return Err(Error::Cleanup); }
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
        let mut copy=ProgressCounter::Published(0);
        assert_eq!(copy.observe("out_time_us=40000"),Ok(false));
        assert_eq!(copy.observe("packet=0"),Ok(false));
        assert_eq!(copy.observe("packet=1"),Ok(true));
        assert_eq!(copy.observe("packet=1"),Ok(false));
        assert_eq!(copy.observe("packet=0"),Err(Error::Encoder));
        assert_eq!(copy.observe("frame=1000"),Ok(false));
        assert_eq!(copy.observe("packet=invalid"),Err(Error::Encoder));
    }
    #[tokio::test] async fn copy_progress_renews_without_an_encoded_frame_counter() {
        let (mut output,input)=tokio::io::duplex(4096);
        let producer=tokio::spawn(async move {
            for packet in [1,2,3,4] {
                output.write_all(format!("packet={packet}\n").as_bytes()).await.unwrap();
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
                if output.write_all(format!("frame={frames}\npacket=1\n").as_bytes()).await.is_err() { return; }
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
            "ffmpeg":"/does/not/exist","publisher":"/also/not/present","width":320,"height":240,"frame_timeout_ms":3000
        })).unwrap();
        let (_tx,mut rx)=watch::channel(true);
        assert_eq!(attempt(&c,"not-used",&Prepared::load(&c).unwrap(),&mut rx).await,Err(Error::Stopped));
    }
}
