//! One JPEG encoder and a copy-only publisher. The bounded Rust stage inserts
//! frame metadata into H.264; it does not implement RTSP/RTP or encode pixels.
use crate::{config::Config, detections::Sample, protocol::Reader, runner, Error, Result};
use detection_wire::{Version, VideoFrame};
use std::{process::Stdio, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::TcpStream,
    process::Command,
    sync::{mpsc, watch},
    time::{timeout, timeout_at, Instant},
};
const UNIT_CAP: usize = 1_048_576;

fn publisher(c: &Config, token: &str) -> Command {
    let mut p = Command::new(&c.ffmpeg);
    p.env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LANG", "C")
        .args([
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-stats_period",
            "0.5",
            "-progress",
            "pipe:1",
            "-f",
            "h264",
            "-probesize",
            "32",
            "-analyzeduration",
            "1",
            "-use_wallclock_as_timestamps",
            "1",
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "copy",
            "-fps_mode",
            "passthrough",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            "-rw_timeout",
            "5000000",
        ])
        .arg(format!(
            "rtsp://{}:{}@{}/{}",
            c.publisher_user, token, c.rtsp, c.stream_path
        ))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    p
}
fn start_code(b: &[u8], from: usize) -> Option<(usize, usize)> {
    (from..b.len().saturating_sub(2)).find_map(|i| {
        if b.get(i..i + 4) == Some(&[0, 0, 0, 1]) {
            Some((i, 4))
        } else if b.get(i..i + 3) == Some(&[0, 0, 1]) {
            Some((i, 3))
        } else {
            None
        }
    })
}
#[derive(Default)]
struct Units {
    pending: Vec<u8>,
    unit: Vec<u8>,
    aud_end: usize,
}
impl Units {
    fn append(&mut self, bytes: &[u8]) -> Result<Vec<(Vec<u8>, usize)>> {
        if self.pending.len() + bytes.len() > UNIT_CAP {
            return Err(Error::Frame);
        }
        self.pending.extend(bytes);
        let mut ready = Vec::new();
        loop {
            let Some((at, prefix)) = start_code(&self.pending, 0) else {
                return Ok(ready);
            };
            if at != 0 {
                return Err(Error::Protocol);
            }
            let Some((end, _)) = start_code(&self.pending, prefix) else {
                return Ok(ready);
            };
            let nal = self.pending[prefix..end].to_vec();
            self.pending.drain(..end);
            if nal.is_empty() {
                return Err(Error::Protocol);
            }
            if nal[0] & 31 == 9 && self.aud_end > 0 {
                ready.push((std::mem::take(&mut self.unit), self.aud_end));
                self.aud_end = 0;
            }
            if self.unit.len() + nal.len() + 4 > UNIT_CAP {
                return Err(Error::Frame);
            }
            if self.aud_end == 0 && nal[0] & 31 != 9 {
                return Err(Error::Protocol);
            }
            self.unit.extend([0, 0, 0, 1]);
            self.unit.extend(&nal);
            if nal[0] & 31 == 9 {
                self.aud_end = self.unit.len();
            }
        }
    }
}
async fn relay<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(
    mut encoded: R,
    mut output: W,
    mut identities: mpsc::Receiver<VideoFrame>,
    budget: Duration,
) -> Result<()> {
    let mut parser = Units::default();
    let mut bytes = [0; 8192];
    let mut deadline = Instant::now() + Duration::from_secs(10);
    loop {
        timeout_at(deadline, async {
            let n = encoded.read(&mut bytes).await?;
            if n == 0 {
                return Err(Error::Encoder);
            }
            for (unit, aud) in parser.append(&bytes[..n])? {
                let frame = identities.try_recv().map_err(|_| Error::Protocol)?;
                let sei = frame.sei().map_err(|_| Error::Protocol)?;
                output.write_all(&unit[..aud]).await?;
                output.write_all(&sei).await?;
                output.write_all(&unit[aud..]).await?;
                deadline = Instant::now() + budget;
            }
            Ok::<(), Error>(())
        })
        .await
        .map_err(|_| Error::Timeout)??;
    }
}
async fn pump(
    mut source: Reader<TcpStream>,
    mut sink: tokio::process::ChildStdin,
    identities: mpsc::Sender<VideoFrame>,
    metadata: watch::Sender<Option<Sample>>,
    epoch: String,
    budget: Duration,
) -> Result<()> {
    let mut lag = runner::Lag::default();
    loop {
        timeout(budget, async {
            let frame = source.next_frame().await?;
            lag.observe(frame, Instant::now())?;
            identities
                .try_send(VideoFrame {
                    api_version: Version::V1,
                    epoch: epoch.clone(),
                    frame_sequence: frame.sequence,
                    frame_capture_ms: frame.capture_ms,
                    source: source.metadata.clone(),
                })
                .map_err(|_| {
                    eprintln!("nxrs-camera-ingest: annotation frame queue exceeded");
                    Error::Stale
                })?;
            sink.write_all(source.payload(frame)).await?;
            if let Some(s) = &source.metadata {
                metadata.send_replace(Some(Sample {
                    frame,
                    source: s.clone(),
                    received: Instant::now(),
                }));
            }
            Ok::<(), Error>(())
        })
        .await
        .map_err(|_| Error::Timeout)??;
    }
}
pub(crate) async fn attempt(
    c: &Config,
    token: &str,
    stop: &mut watch::Receiver<bool>,
) -> Result<()> {
    let source = tokio::select! {biased;_=stop.changed()=>return Err(Error::Stopped),r=timeout(Duration::from_secs(5),async {
        let mut socket=TcpStream::connect(c.camera).await?;socket.set_nodelay(true)?;
        socket.write_all(format!("GET /stream?metadata=1 HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n",c.camera).as_bytes()).await?;
        Reader::start(socket,c.width,c.height).await
    })=>r.map_err(|_|Error::Timeout)??};
    let mut bytes = [0; 16];
    getrandom::fill(&mut bytes).map_err(|_| Error::Configuration)?;
    let epoch: String = bytes.iter().map(|v| format!("{v:02x}")).collect();
    let mut encoder = runner::encoder(c, token, true)
        .spawn()
        .map_err(|_| Error::Encoder)?;
    let mut publisher = match publisher(c, token).spawn() {
        Ok(p) => p,
        Err(_) => {
            runner::retire(&mut encoder).await?;
            return Err(Error::Encoder);
        }
    };
    let (tx, rx) = mpsc::channel(32);
    let (metadata, watch) = watch::channel(None);
    let (end, ended) = watch::channel(false);
    let detection = c
        .detections
        .clone()
        .expect("annotated path requires detection configuration");
    let forwarding = tokio::spawn(crate::detections::run(
        detection,
        c.publisher_user.clone(),
        token.to_owned(),
        watch,
        ended,
    ));
    let outcome = match (
        encoder.stdin.take(),
        encoder.stdout.take(),
        encoder.stderr.take(),
        publisher.stdin.take(),
        publisher.stdout.take(),
    ) {
        (Some(input), Some(encoded), Some(progress), Some(output), Some(published)) => {
            let budget = Duration::from_millis(c.frame_timeout_ms);
            tokio::select! {biased;_=stop.changed()=>Err(Error::Stopped),_=encoder.wait()=>Err(Error::Encoder),_=publisher.wait()=>Err(Error::Encoder),
            r=pump(source,input,tx,metadata,epoch,budget)=>r,
            r=relay(encoded,output,rx,budget.max(Duration::from_secs(5)))=>r,
            r=runner::encoder_progress(progress,budget.max(Duration::from_secs(3)))=>r,
            r=runner::publisher_progress(published,budget.max(Duration::from_secs(3)))=>r}
        }
        _ => Err(Error::Encoder),
    };
    let _ = end.send(true);
    let forward = forwarding.await;
    // Attempt both barriers even if one child fails to reap. A second source
    // is never admitted until both old children and the metadata task retire.
    let a = runner::retire(&mut encoder).await;
    let b = runner::retire(&mut publisher).await;
    if a.is_err() || b.is_err() || forward.is_err() {
        return Err(Error::Cleanup);
    }
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn delimiters_across_every_byte_boundary_keep_complete_units() {
        let input = [
            0, 0, 0, 1, 9, 0xf0, 0, 0, 1, 0x65, 0x88, 0, 0, 0, 1, 9, 0xf0, 0, 0, 1, 0x41, 0x99, 0,
            0, 0, 1, 9, 0xf0, 0, 0, 1, 0x41, 0xaa,
        ];
        let mut parser = Units::default();
        let mut ready = Vec::new();
        for byte in input {
            ready.extend(parser.append(&[byte]).unwrap());
        }
        assert_eq!(ready.len(), 2);
        assert_eq!(ready[0].1, 6);
        assert_eq!(ready[0].0, [0, 0, 0, 1, 9, 0xf0, 0, 0, 0, 1, 0x65, 0x88]);
    }
    #[test]
    fn missing_aud_and_oversized_units_fail_closed() {
        assert!(Units::default()
            .append(&[0, 0, 1, 0x65, 0x88, 0, 0, 1])
            .is_err());
        assert!(Units::default().append(&vec![0; UNIT_CAP + 1]).is_err());
    }
    #[tokio::test]
    async fn partial_bytes_do_not_renew_a_complete_frame_deadline() {
        let (mut producer, encoded) = tokio::io::duplex(8192);
        let (output, _consumer) = tokio::io::duplex(16384);
        let (tx, rx) = mpsc::channel(2);
        tx.send(VideoFrame {
            api_version: Version::V1,
            epoch: "0123456789abcdef0123456789abcdef".into(),
            frame_sequence: 1,
            frame_capture_ms: 1,
            source: None,
        })
        .await
        .unwrap();
        let writer = tokio::spawn(async move {
            // A complete frame followed by an indefinitely incomplete NAL.
            producer
                .write_all(&[
                    0, 0, 1, 9, 0xf0, 0, 0, 1, 0x65, 0x88, 0, 0, 1, 9, 0xf0, 0, 0, 1, 0x41,
                ])
                .await
                .unwrap();
            loop {
                tokio::time::sleep(Duration::from_millis(5)).await;
                if producer.write_all(&[7]).await.is_err() {
                    break;
                }
            }
        });
        let result = timeout(
            Duration::from_secs(1),
            relay(encoded, output, rx, Duration::from_millis(40)),
        )
        .await;
        writer.abort();
        assert!(matches!(result, Ok(Err(Error::Timeout))));
    }
}
