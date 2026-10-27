use crate::{tls::Prepared, config::{Config, EncodedMedia}, protocol::{line, Reader, Frame}, Error, Result};
use std::{process::Stdio, time::Duration};
use tokio::{io::{AsyncReadExt, AsyncWriteExt, BufReader}, net::TcpStream, process::{Command, Child},
    sync::watch, time::{Instant, timeout, timeout_at, sleep}};

const STARTUP_OUTPUT:Duration = Duration::from_secs(10);
/// Probe the same installed helper before loading source/encoder resources.
/// This owns a disposable child; failed enforcement never falls back.
pub async fn check_encoder_isolation(launcher: &std::path::Path) -> Result<()> {
    nxrs_media_process::probe(launcher).await.map_err(|error| match error {
        nxrs_media_process::Error::Cleanup => Error::Cleanup,
        nxrs_media_process::Error::Unavailable => Error::Configuration,
    })
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

pub(crate) fn encoder(config: &Config, format: EncodedMedia) -> Command {
    let mut command = match &config.media_sandbox {
        Some(launcher) => Command::from(nxrs_media_sandbox::command(launcher, &config.ffmpeg, None)),
        None => Command::new(&config.ffmpeg),
    };
    // No shell, environment-proxy fallback, image looping, or constant-rate frame duplication.
    command.env_clear().env("PATH","/usr/bin:/bin").env("LANG","C")
        .args(["-nostdin","-hide_banner","-loglevel","quiet","-nostats","-stats_period","0.5",
            "-xerror","-err_detect","explode","-progress","pipe:2","-threads","2","-f","image2pipe","-c:v","mjpeg",
            "-framerate","30","-probesize",if format == EncodedMedia::AnnexB {"32"} else {"153600"},
            "-analyzeduration",if format == EncodedMedia::AnnexB {"1"} else {"0"},
            "-use_wallclock_as_timestamps","1","-i","pipe:0","-an","-c:v","libx264",
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
pub(crate) async fn encoder_progress<R: tokio::io::AsyncRead + Unpin>(output: R, budget: Duration) -> Result<()> {
    publication_progress(output, "frame=", budget).await
}
pub(crate) async fn publisher_progress<R: tokio::io::AsyncRead + Unpin>(output: R, budget: Duration) -> Result<()> {
    publication_progress(output, "packet=", budget).await
}
pub(crate) fn publisher(config: &Config, format: EncodedMedia) -> Command {
    let mut command=Command::new(&config.publisher);
    command.env_clear().env("PATH","/usr/bin:/bin").env("LANG","C")
        .args([config.rtsp.to_string(),config.publisher_user.clone(),config.stream_path.clone()]);
    if format == EncodedMedia::AnnexB { command.arg("annexb"); }
    command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true);
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
pub(crate) async fn retire(child: &mut Child) -> Result<()> {
    nxrs_media_process::retire(child).await.map_err(|_| Error::Cleanup)
}
pub(crate) async fn camera_source(config:&Config, policy:&Prepared, stop:&mut watch::Receiver<bool>) -> Result<Reader<crate::tls::Socket>> {
    if *stop.borrow() { return Err(Error::Stopped); }
    tokio::select! {
        biased;
        _=stop.changed()=>Err(Error::Stopped),
        error=policy.guard()=>Err(error),
        result=observed(Stage::CameraSetup,async {timeout(Duration::from_secs(5),async {
            let socket=TcpStream::connect(config.camera).await?;
            socket.set_nodelay(true)?;
            let mut socket=policy.connect(socket).await?;
            let path=if config.detections.is_some() { "/stream?metadata=1" } else { "/stream" };
            let request=format!("GET {path} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n",config.camera);
            socket.write_all(request.as_bytes()).await?;
            Reader::start(socket,config.width,config.height).await
        }).await.map_err(|_|Error::Timeout)?})=> result,
    }
}
async fn attempt(config:&Config, token:&str, policy:&Prepared, stop:&mut watch::Receiver<bool>) -> Result<()> {
    if *stop.borrow() { return Err(Error::Stopped); }
    if config.detections.is_some() { return crate::video::attempt(config, token, policy, stop).await; }
    let source=camera_source(config,policy,stop).await?;
    let mut publication=publisher(config,EncodedMedia::MpegTs).spawn().map_err(|_|Error::Encoder)?;
    let mut child=match encoder(config,EncodedMedia::MpegTs).spawn() {
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
                result=observed(Stage::EncoderProgress,encoder_progress(progress,budget.max(Duration::from_secs(3))))=>result,
                result=observed(Stage::PublisherProgress,publisher_progress(published,budget.max(Duration::from_secs(3))))=>result,
            }
        },
        _=>Err(Error::Encoder),
    };
    let (encoder_retired,publisher_retired)=tokio::join!(retire(&mut child),retire(&mut publication));
    encoder_retired?;
    publisher_retired?;
    outcome
}
/// The source and children are retired, but the loaded enrollment is still a
/// lease. Expiry and clock failure must stop supervision during backoff too.
async fn wait_to_retry(
    delay: Duration,
    stop: &mut watch::Receiver<bool>,
    lease: impl std::future::Future<Output = Error>,
) -> Result<()> {
    if *stop.borrow() { return Err(Error::Stopped); }
    tokio::select! {
        biased;
        _=stop.changed()=>Err(Error::Stopped),
        error=lease=>Err(error),
        _=sleep(delay)=>Ok(()),
    }
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
        match wait_to_retry(Duration::from_secs(delay),&mut stop,policy.guard()).await {
            Ok(())=>{},
            Err(Error::Stopped)=>return Ok(()),
            Err(error)=>return Err(error),
        }
        delay=(delay*2).min(8);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn credential_failure_interrupts_the_maximum_reconnect_backoff() {
        let (_stop,mut rx)=watch::channel(false);
        let (expire,expired)=tokio::sync::oneshot::channel();
        let waiting=tokio::spawn(async move {
            wait_to_retry(Duration::from_secs(8),&mut rx,async {
                expired.await.unwrap();
                Error::Authentication
            }).await
        });
        expire.send(()).unwrap();
        assert_eq!(timeout(Duration::from_millis(250),waiting).await.unwrap().unwrap(),
                   Err(Error::Authentication));
    }
    #[tokio::test]
    async fn reconnect_wait_keeps_stop_priority_and_development_retry_behavior() {
        let (stop,mut rx)=watch::channel(true);
        assert_eq!(wait_to_retry(Duration::from_secs(8),&mut rx,async { panic!("cancelled lease polled") }).await,
                   Err(Error::Stopped));
        stop.send(false).unwrap();
        rx.borrow_and_update();
        let waiting=tokio::spawn(async move {
            wait_to_retry(Duration::from_secs(8),&mut rx,std::future::pending()).await
        });
        stop.send(true).unwrap();
        assert_eq!(timeout(Duration::from_millis(250),waiting).await.unwrap().unwrap(),Err(Error::Stopped));
        let (_stop,mut rx)=watch::channel(false);
        assert_eq!(wait_to_retry(Duration::from_millis(1),&mut rx,std::future::pending()).await,Ok(()));
    }
    #[tokio::test]
    async fn sdk_packet_progress_renews_without_encoder_frame_or_output_time_fields() {
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
        producer.abort();let _=producer.await;
        assert_eq!(outcome.unwrap(),Err(Error::Encoder));
        assert!(start.elapsed()>=Duration::from_millis(600));
    }
    #[tokio::test]
    async fn duplicate_packets_encoder_frames_and_mux_times_cannot_renew_publication() {
        let (mut output,input)=tokio::io::duplex(4096);
        let producer=tokio::spawn(async move {
            let mut unrelated=1;
            loop {
                if output.write_all(format!("frame={unrelated}\nout_time_us={unrelated}\npacket=1\n").as_bytes()).await.is_err(){return;}
                unrelated+=1;sleep(Duration::from_millis(25)).await;
            }
        });
        let outcome=timeout(Duration::from_secs(2),publisher_progress(input,Duration::from_millis(250))).await;
        producer.abort();let _=producer.await;
        assert_eq!(outcome.unwrap(),Err(Error::Encoder));
    }
    #[tokio::test]
    async fn invalid_and_regressing_sdk_packet_counters_refuse() {
        for data in ["packet=1\npacket=0\n","packet=-1\n","packet=18446744073709551616\n","packet=invalid\n"] {
            let (mut writer,input)=tokio::io::duplex(128);
            writer.write_all(data.as_bytes()).await.unwrap();
            assert_eq!(timeout(Duration::from_millis(250),publisher_progress(input,Duration::from_secs(3))).await.unwrap(),Err(Error::Encoder));
        }
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
            "ffmpeg":"/does/not/exist","publisher":"/does/not/exist-either","width":320,"height":240,"frame_timeout_ms":3000
        })).unwrap();
        let (_tx,mut rx)=watch::channel(true);
        assert_eq!(attempt(&c,"not-used",&Prepared::load(&c).unwrap(),&mut rx).await,Err(Error::Stopped));
    }
}
