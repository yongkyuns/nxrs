//! One authenticated loopback RTSP reader. Original H.264 is remuxed; 96x72 gray
//! samples are decoded at 2 Hz for motion. There is never another camera connection.
use crate::{
    identifier,
    motion::{Detector, PIXELS},
    now_ms,
    progress::FrameProgress,
    store::{Error, Health, Settings, Store, MAX_CLIP_BYTES, MAX_THUMBNAIL_BYTES},
};
use serde::Deserialize;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::{
    collections::VecDeque,
    fs,
    io::Read,
    net::SocketAddrV4,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, Command},
    sync::{mpsc, watch},
    time::{timeout, Instant},
};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub storage: Settings,
    pub camera_id: String,
    pub rtsp: SocketAddrV4,
    pub stream_path: String,
    pub username: String,
    pub token_file: PathBuf,
    pub ffmpeg: PathBuf,
    pub reader: PathBuf,
    pub media_sandbox: Option<PathBuf>,
    pub motion_percent: u16,
    pub pre_roll_seconds: u64,
    pub post_roll_seconds: u64,
    pub max_clip_seconds: u64,
}
impl Config {
    pub fn validate(&self) -> Result<(), Error> {
        self.storage.validate()?;
        if !self.storage.camera_ids.contains(&self.camera_id)
            || !identifier(&self.camera_id)
            || !identifier(&self.stream_path)
            || !identifier(&self.username)
            || !self.rtsp.ip().is_loopback()
            || self.rtsp.port() == 0
            || !self.reader.is_absolute()
            || self
                .media_sandbox
                .as_ref()
                .is_some_and(|p| !p.is_absolute())
            || !self.ffmpeg.is_absolute()
            || !self.token_file.is_absolute()
            || !(1..=50).contains(&self.motion_percent)
            || self.pre_roll_seconds > 10
            || !(2..=20).contains(&self.post_roll_seconds)
            || !(10..=60).contains(&self.max_clip_seconds)
            || self.pre_roll_seconds + self.post_roll_seconds >= self.max_clip_seconds
            || self
                .storage
                .directory
                .to_string_lossy()
                .contains(['\n', '\r', '\'', '"', ','])
        {
            return Err(Error::Invalid);
        }
        Ok(())
    }
    pub fn load(path: &Path) -> Result<Self, Error> {
        let bytes = private_read(path, 16_384)?;
        let config: Self = serde_json::from_slice(&bytes).map_err(|_| Error::Invalid)?;
        config.validate()?;
        config.check_isolation(path, false)?;
        Ok(config)
    }
    pub fn check_isolation(&self, path: &Path, required: bool) -> Result<(), Error> {
        if required && self.media_sandbox.is_none() {
            return Err(Error::Invalid);
        }
        if self.media_sandbox.is_some() {
            nxrs_media_sandbox::check_credentials(&self.ffmpeg, &[path, &self.token_file], None)
                .map_err(|_| Error::Invalid)?;
        }
        Ok(())
    }
    fn token(&self) -> Result<zeroize::Zeroizing<String>, Error> {
        let bytes = zeroize::Zeroizing::new(private_read(&self.token_file, 128)?);
        let token = std::str::from_utf8(&bytes)
            .map_err(|_| Error::Invalid)?
            .trim_end_matches('\n');
        if token.len() != 64
            || !token
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(Error::Invalid);
        }
        Ok(zeroize::Zeroizing::new(token.into()))
    }
}
fn private_read(path: &Path, max: u64) -> Result<Vec<u8>, Error> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() || meta.nlink() != 1 || meta.mode() & 0o077 != 0 || meta.len() > max {
        return Err(Error::Invalid);
    }
    let mut bytes = Vec::new();
    file.take(max + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > max {
        return Err(Error::Invalid);
    }
    Ok(bytes)
}
/// Native output is adversarial. Open the inode once without following links;
/// metadata-before-open followed by fs::read/copy would be a confused deputy.
fn native_file(path: &Path) -> Result<fs::File, Error> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() || meta.nlink() != 1 {
        return Err(Error::Corrupt);
    }
    Ok(file)
}
fn native_bytes(path: &Path, max: u64) -> Result<Vec<u8>, Error> {
    let file = native_file(path)?;
    if file.metadata()?.len() > max {
        return Err(Error::Full);
    }
    let mut bytes = Vec::new();
    file.take(max + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > max {
        return Err(Error::Full);
    }
    Ok(bytes)
}
fn random_id() -> Result<String, Error> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| Error::Io)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
async fn private_input(
    child: &mut Child,
    credential: &[u8],
    stop: &mut watch::Receiver<bool>,
) -> Result<(), Error> {
    let mut input = child.stdin.take().ok_or(Error::Io)?;
    tokio::select! {
        biased;
        _ = stop.changed() => Err(Error::Io),
        result = timeout(Duration::from_secs(1), input.write_all(credential)) => {
            result.map_err(|_| Error::Io)?.map_err(Error::from)
        },
    }
    // Only the remux reader receives these 64 bytes. Dropping the sole writer
    // supplies EOF; decoder input is a separate compressed-media-only pipe.
}

fn command(config: &Config, work: &Path) -> Result<Command, Error> {
    let mut c = match &config.media_sandbox {
        Some(launcher) => {
            nxrs_media_sandbox::check_credentials(
                &config.ffmpeg,
                &[&config.token_file],
                Some(work),
            )
            .map_err(|_| Error::Invalid)?;
            Command::from(nxrs_media_sandbox::command(
                launcher,
                &config.ffmpeg,
                Some(work),
            ))
        }
        None => Command::new(&config.ffmpeg),
    };
    c.env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LANG", "C")
        .args([
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "quiet",
            "-threads",
            "2",
            "-filter_threads",
            "1",
        ])
        .stdin(Stdio::null())
        .kill_on_drop(true);
    Ok(c)
}
async fn reap(child: &mut Child) -> Result<(), Error> {
    nxrs_media_process::retire(child)
        .await
        .map_err(|_| Error::Cleanup)
}

async fn relay_media(
    mut input: tokio::process::ChildStdout,
    mut output: tokio::process::ChildStdin,
) -> Result<(), Error> {
    let mut bytes = [0u8; 4096];
    let mut first = true;
    loop {
        let budget = Duration::from_secs(if first { 15 } else { 8 });
        timeout(budget, async {
            let count = input.read(&mut bytes).await?;
            if count == 0 {
                return Err(Error::Io);
            }
            output.write_all(&bytes[..count]).await?;
            Ok(())
        })
        .await
        .map_err(|_| Error::Io)??;
        first = false;
    }
}
async fn reader_progress(input: tokio::process::ChildStderr) -> Result<(), Error> {
    let mut input = BufReader::new(input);
    let mut count = 0u64;
    loop {
        let mut line = Vec::new();
        timeout(
            Duration::from_secs(if count == 0 { 15 } else { 8 }),
            (&mut input).take(65).read_until(b'\n', &mut line),
        )
        .await
        .map_err(|_| Error::Io)??;
        count = count.checked_add(1).ok_or(Error::Io)?;
        if line != format!("packet={count}\n").as_bytes() {
            return Err(Error::Io);
        }
    }
}

#[derive(Clone)]
struct Segment {
    path: PathBuf,
    start: u64,
    end: u64,
}
enum Input {
    Gray(Box<[u8; PIXELS]>),
    Segment(Segment),
}

async fn gray_frames(
    mut input: tokio::process::ChildStderr,
    sender: mpsc::Sender<Input>,
) -> Result<(), Error> {
    loop {
        let mut gray = Box::new([0; PIXELS]);
        input.read_exact(gray.as_mut_slice()).await?;
        sender
            .send(Input::Gray(gray))
            .await
            .map_err(|_| Error::Io)?;
    }
}
async fn segments(
    input: tokio::process::ChildStdout,
    directory: PathBuf,
    sender: mpsc::Sender<Input>,
) -> Result<(), Error> {
    let mut input = BufReader::new(input);
    loop {
        let mut line = Vec::new();
        let read = (&mut input).take(1025).read_until(b'\n', &mut line).await?;
        if read == 0 || line.len() > 1024 || line.last() != Some(&b'\n') {
            return Err(Error::Io);
        }
        let line = std::str::from_utf8(&line)
            .map_err(|_| Error::Corrupt)?
            .trim();
        let mut fields = line.rsplitn(3, ',');
        let end: f64 = fields
            .next()
            .ok_or(Error::Corrupt)?
            .parse()
            .map_err(|_| Error::Corrupt)?;
        let start: f64 = fields
            .next()
            .ok_or(Error::Corrupt)?
            .parse()
            .map_err(|_| Error::Corrupt)?;
        let path = PathBuf::from(fields.next().ok_or(Error::Corrupt)?.trim_matches('"'));
        let path = if path.components().count() == 1 {
            directory.join(path)
        } else {
            path
        };
        if path.parent() != Some(directory.as_path())
            || !path.file_name().and_then(|p| p.to_str()).is_some_and(|p| {
                p.starts_with("part-")
                    && p.ends_with(".ts")
                    && p.len() == 14
                    && p[5..11].bytes().all(|b| b.is_ascii_digit())
            })
            || !start.is_finite()
            || !end.is_finite()
            || !(0.01..=10.0).contains(&(end - start))
        {
            return Err(Error::Corrupt);
        }
        let observed = now_ms();
        sender
            .send(Input::Segment(Segment {
                path,
                start: observed.saturating_sub(((end - start) * 1000.0) as u64),
                end: observed,
            }))
            .await
            .map_err(|_| Error::Io)?;
    }
}

struct Window {
    start: u64,
    started: Instant,
    last_motion: Instant,
}
impl Window {
    fn complete(&self, now: Instant, config: &Config) -> bool {
        now.saturating_duration_since(self.last_motion)
            >= Duration::from_secs(config.post_roll_seconds)
            || now.saturating_duration_since(self.started)
                >= Duration::from_secs(config.max_clip_seconds)
    }
}

async fn encode_clip(
    config: Config,
    store: Store,
    selected: Vec<Segment>,
    interrupted: bool,
    mut stop: watch::Receiver<bool>,
) -> Result<(), Error> {
    if selected.is_empty() {
        return Err(Error::Invalid);
    }
    config.storage.verify_protection()?;
    let job = config.storage.directory.join("spool").join(format!(
        "job-{}-{}",
        config.camera_id,
        random_id()?
    ));
    fs::DirBuilder::new().mode(0o700).create(&job)?;
    let result = async {
        let mut list = String::new();
        let mut total = 0;
        for (i, segment) in selected.iter().enumerate() {
            let file = native_file(&segment.path)?;
            let remaining = MAX_CLIP_BYTES - total;
            if file.metadata()?.len() > remaining {
                return Err(Error::Full);
            }
            let name = format!("part-{i:03}.ts");
            let mut destination = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(job.join(&name))?;
            let copied = std::io::copy(&mut file.take(remaining + 1), &mut destination)?;
            if copied > remaining {
                return Err(Error::Full);
            }
            total += copied;
            list.push_str(&format!("file '{name}'\n"));
        }
        fs::write(job.join("inputs.txt"), list)?;
        let clip = job.join("clip.mp4");
        let image = job.join("thumbnail.jpg");
        let mut child = command(&config, &job)?
            .args([
                "-f",
                "concat",
                "-safe",
                "1",
                "-protocol_whitelist",
                "file",
                "-i",
            ])
            .arg(job.join("inputs.txt"))
            .args([
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "copy",
                "-movflags",
                "+faststart",
                "-fs",
                "33554432",
            ])
            .arg(&clip)
            .args([
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=320:-2",
                "-q:v",
                "5",
            ])
            .arg(&image)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let exit = tokio::select! {
            _ = stop.changed() => Err(Error::Io),
            exit = timeout(Duration::from_secs(20), child.wait()) => match exit {
                Ok(result) => result.map_err(Error::from), Err(_) => Err(Error::Io),
            },
        };
        reap(&mut child).await?;
        if !exit?.success() {
            return Err(Error::Io);
        }
        let clip = native_bytes(&clip, MAX_CLIP_BYTES)?;
        let image = native_bytes(&image, MAX_THUMBNAIL_BYTES)?;
        if !(1..MAX_CLIP_BYTES).contains(&(clip.len() as u64)) || image.is_empty() {
            return Err(Error::Full);
        }
        let start = selected.first().ok_or(Error::Invalid)?.start;
        let end = selected.last().ok_or(Error::Invalid)?.end;
        tokio::task::spawn_blocking(move || {
            store.publish(&config.camera_id, start, end, &clip, &image, interrupted)
        })
        .await
        .map_err(|_| Error::Io)??;
        Ok(())
    }
    .await;
    if result != Err(Error::Cleanup) {
        let _ = fs::remove_dir_all(job);
    }
    result
}

async fn attempt(
    config: &Config,
    store: &Store,
    token: &str,
    stop: &mut watch::Receiver<bool>,
    health: &mut Health,
    progress: &mut FrameProgress,
) -> Result<(), Error> {
    if *stop.borrow() {
        return Ok(());
    }
    config.storage.verify_protection()?;
    let mut detector = Detector::new(config.motion_percent).map_err(|_| Error::Invalid)?;
    let directory = config.storage.directory.join("spool").join(format!(
        "source-{}-{}",
        config.camera_id,
        random_id()?
    ));
    fs::DirBuilder::new().mode(0o700).create(&directory)?;
    let child = (|| -> Result<Child, Error> {
        command(config, &directory)?
            .args([
                "-f",
                "mpegts",
                "-protocol_whitelist",
                "pipe",
                "-probesize",
                "32768",
                "-analyzeduration",
                "1000000",
                "-i",
                "pipe:0",
            ])
            .args([
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                "2",
                "-segment_format",
                "mpegts",
                "-segment_list",
                "pipe:1",
                "-segment_list_type",
                "csv",
                "-reset_timestamps",
                "1",
            ])
            .arg(directory.join("part-%06d.ts"))
            .args([
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                "fps=2,scale=96:72",
                "-pix_fmt",
                "gray",
                "-threads",
                "1",
                "-f",
                "rawvideo",
                "pipe:2",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(Error::from)
    })();
    let mut child = match child {
        Ok(child) => child,
        Err(_) => {
            let _ = fs::remove_dir_all(&directory);
            return Err(Error::Io);
        }
    };
    let source = Command::new(&config.reader)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LANG", "C")
        .args([
            config.rtsp.to_string(),
            config.username.clone(),
            config.stream_path.clone(),
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn();
    let mut source = match source {
        Ok(source) => source,
        Err(_) => {
            reap(&mut child).await?;
            let _ = fs::remove_dir_all(&directory);
            return Err(Error::Io);
        }
    };
    let setup = private_input(&mut source, token.as_bytes(), stop).await;
    let handles = (
        child.stdout.take(),
        child.stderr.take(),
        child.stdin.take(),
        source.stdout.take(),
        source.stderr.take(),
    );
    let (a, b, sink, compressed, status) = match (setup, handles) {
        (Ok(()), (Some(a), Some(b), Some(sink), Some(compressed), Some(status))) => {
            (a, b, sink, compressed, status)
        }
        _ => {
            let (decoder, reader) = tokio::join!(reap(&mut child), reap(&mut source));
            decoder?;
            reader?;
            let _ = fs::remove_dir_all(&directory);
            return Err(Error::Io);
        }
    };
    let mut relay = tokio::spawn(relay_media(compressed, sink));
    let mut status_reader = tokio::spawn(reader_progress(status));
    let mut relay_finished = false;
    let mut status_finished = false;
    let (sender, mut input) = mpsc::channel(8);
    let mut segment_reader = tokio::spawn(segments(a, directory.clone(), sender.clone()));
    let mut gray_reader = tokio::spawn(gray_frames(b, sender));
    let mut segments_finished = false;
    let mut gray_finished = false;
    let mut ring = VecDeque::<Segment>::new();
    let mut window: Option<Window> = None;
    let mut job: Option<tokio::task::JoinHandle<Result<(), Error>>> = None;
    let mut pinned = Vec::<PathBuf>::new();
    let mut last_frame = Instant::now();
    let mut last_health = Instant::now();
    let mut last_prune = Instant::now();
    let mut seen_frame = false;
    let mut tick = tokio::time::interval(Duration::from_millis(500));
    let result = async {
        loop {
            tokio::select! {
                biased;
                _ = stop.changed() => break Ok(()),
                _ = child.wait() => { eprintln!("nxrs-camera-activity: decoder exited"); break Err(Error::Io); },
                _ = source.wait() => { eprintln!("nxrs-camera-activity: reader exited"); break Err(Error::Io); },
                _ = &mut relay => { relay_finished = true; eprintln!("nxrs-camera-activity: relay stopped"); break Err(Error::Io); },
                _ = &mut status_reader => { status_finished = true; eprintln!("nxrs-camera-activity: status reader stopped"); break Err(Error::Io); },
                _ = &mut segment_reader => { segments_finished = true; eprintln!("nxrs-camera-activity: segment reader stopped"); break Err(Error::Io); },
                _ = &mut gray_reader => { gray_finished = true; eprintln!("nxrs-camera-activity: gray reader stopped"); break Err(Error::Io); },
                event = input.recv() => match event.ok_or(Error::Io)? {
                    Input::Gray(gray) => {
                        last_frame = Instant::now(); seen_frame = true; health.frames_observed += 1; health.last_frame_at_ms = Some(now_ms());
                        progress.frame(last_frame); health.progress = Some(progress.health());
                        if detector.observe(&gray) {
                            let now = now_ms();
                            let instant = Instant::now();
                            let active = window.get_or_insert(Window { start: now.saturating_sub(config.pre_roll_seconds*1000),
                                started: instant - Duration::from_secs(config.pre_roll_seconds), last_motion: instant });
                            active.last_motion = instant;
                        }
                    },
                    Input::Segment(segment) => {
                        ring.push_back(segment);
                        let now = now_ms();
                        if window.as_ref().is_some_and(|w| w.complete(Instant::now(), config)) {
                            let w = window.take().ok_or(Error::Io)?;
                            let selected: Vec<_> = ring.iter().filter(|s| s.end >= w.start).cloned().collect();
                            if job.is_none() && !selected.is_empty() {
                                pinned = selected.iter().map(|s| s.path.clone()).collect();
                                job = Some(tokio::spawn(encode_clip(config.clone(), store.clone(), selected, false, stop.clone())));
                            } else { health.clips_dropped += 1; }
                        }
                        let keep_after = window.as_ref().map(|w| w.start)
                            .unwrap_or_else(|| now.saturating_sub((config.pre_roll_seconds+4)*1000));
                        while ring.front().is_some_and(|s| s.end < keep_after) {
                            if ring.front().is_some_and(|s| pinned.contains(&s.path)) { break; }
                            let s = ring.pop_front().ok_or(Error::Io)?; fs::remove_file(s.path)?;
                        }
                        if ring.len() > 40 { return Err(Error::Full); }
                    },
                },
                _ = tick.tick() => {
                    let budget = if !seen_frame { 15 } else { 8 };
                    if last_frame.elapsed() > Duration::from_secs(budget) { return Err(Error::Io); }
                    let mut count = 0; let mut bytes = 0u64;
                    for entry in fs::read_dir(&directory)?.take(42) {
                        let entry = entry?; count += 1;
                        let meta = fs::symlink_metadata(entry.path())?;
                        if !meta.is_file() || meta.nlink() != 1 { return Err(Error::Corrupt); }
                        bytes += meta.len();
                    }
                    if count > 40 || bytes > 64 * 1024 * 1024 { return Err(Error::Full); }
                    if job.as_ref().is_some_and(|j| j.is_finished()) {
                        match job.take().ok_or(Error::Io)?.await.map_err(|_| Error::Io)? {
                            Ok(()) => { health.clips_created += 1; health.state = "monitoring".into(); },
                            Err(error @ (Error::Cleanup | Error::Protection)) => return Err(error),
                            Err(e) => { health.clips_dropped += 1; health.state = if e == Error::Full { "storage_full" } else { "recording_error" }.into(); },
                        }
                        pinned.clear();
                    }
                    if last_health.elapsed() >= Duration::from_secs(5) {
                        health.updated_at_ms = now_ms();
                        if seen_frame && !matches!(health.state.as_str(), "storage_full" | "recording_error") { health.state = "monitoring".into(); }
                        match store.write_health(&config.camera_id, health) { Ok(()) | Err(Error::Busy) => {}, Err(e) => return Err(e) }
                        last_health = Instant::now();
                    }
                    if last_prune.elapsed() >= Duration::from_secs(60) {
                        match store.prune(now_ms()) { Ok(()) | Err(Error::Busy) => {}, Err(e) => return Err(e) }
                        last_prune = Instant::now();
                    }
                }
            }
        }
    }.await;
    // Both credentialed reader and decoder must retire before reconnect.
    let (reaped, reader_reaped) = tokio::join!(reap(&mut child), reap(&mut source));
    if !relay_finished {
        relay.abort();
        let _ = relay.await;
    }
    if !status_finished {
        status_reader.abort();
        let _ = status_reader.await;
    }
    if !segments_finished {
        segment_reader.abort();
        let _ = segment_reader.await;
    }
    if !gray_finished {
        gray_reader.abort();
        let _ = gray_reader.await;
    }
    let mut cleanup_failed =
        reaped.is_err() || reader_reaped.is_err() || result == Err(Error::Cleanup);
    let mut protection_failed = result == Err(Error::Protection);
    if let Some(job) = job {
        match job.await {
            Ok(Ok(())) => health.clips_created += 1,
            Ok(Err(Error::Cleanup)) => {
                health.clips_dropped += 1;
                cleanup_failed = true;
            }
            Ok(Err(Error::Protection)) => {
                health.clips_dropped += 1;
                protection_failed = true;
            }
            _ => health.clips_dropped += 1,
        }
    }
    if cleanup_failed {
        return Err(Error::Cleanup);
    }
    if protection_failed {
        return Err(Error::Protection);
    }
    if let Some(window) = window {
        let selected: Vec<_> = ring
            .iter()
            .filter(|s| s.end >= window.start)
            .cloned()
            .collect();
        if !selected.is_empty() {
            match encode_clip(config.clone(), store.clone(), selected, true, stop.clone()).await {
                Ok(()) => health.clips_created += 1,
                Err(error @ (Error::Cleanup | Error::Protection)) => return Err(error),
                Err(_) => health.clips_dropped += 1,
            }
        }
    }
    let _ = fs::remove_dir_all(directory);
    reaped?;
    result
}

/// Controller reads share the short-lived store lock. Retry only Busy during
/// startup, before admitting a source; corruption/protection errors remain fatal.
async fn admit_store<T>(
    deadline: Instant,
    stop: &mut watch::Receiver<bool>,
    mut operation: impl FnMut() -> Result<T, Error>,
) -> Result<Option<T>, Error> {
    loop {
        if *stop.borrow() {
            return Ok(None);
        }
        match operation() {
            Ok(value) => return Ok(Some(value)),
            Err(Error::Busy) if Instant::now() < deadline => {},
            Err(error) => return Err(error),
        }
        let next = (Instant::now() + Duration::from_millis(25)).min(deadline);
        tokio::select! {
            biased;
            _ = stop.changed() => return Ok(None),
            _ = tokio::time::sleep_until(next) => {},
        }
    }
}

pub async fn run(config: Config, mut stop: watch::Receiver<bool>) -> Result<(), Error> {
    config.validate()?;
    let token = config.token()?;
    let deadline = Instant::now() + Duration::from_secs(1);
    let Some(store) = admit_store(deadline, &mut stop, || Store::open(config.storage.clone())).await? else {
        return Ok(());
    };
    // Exclusive lifetime lease: a second observer cannot compete for the same camera.
    let lease = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(
            config
                .storage
                .directory
                .join(format!("worker-{}.lock", config.camera_id)),
        )?;
    let metadata = lease.metadata()?;
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.mode() & 0o077 != 0
        || metadata.len() != 0
    {
        return Err(Error::Invalid);
    }
    lease.try_lock().map_err(|_| Error::Busy)?;
    // After a process crash, only this camera's generated spool is abandoned.
    // Its lifetime lease proves that no active worker can still own these paths.
    let source_prefix = format!("source-{}-", config.camera_id);
    let job_prefix = format!("job-{}-", config.camera_id);
    for entry in fs::read_dir(config.storage.directory.join("spool"))?.take(1025) {
        let entry = entry?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| Error::Corrupt)?;
        if let Some((prefix, id)) = name.rsplit_once('-').filter(|(prefix, _)| {
            *prefix == source_prefix.trim_end_matches('-')
                || *prefix == job_prefix.trim_end_matches('-')
        }) {
            let _ = prefix;
            if !crate::event_id(id)
                || !entry.file_type()?.is_dir()
                || entry.file_type()?.is_symlink()
            {
                return Err(Error::Corrupt);
            }
            fs::remove_dir_all(entry.path())?;
        }
    }
    let Some(health) = admit_store(deadline, &mut stop, || store.health(&config.camera_id)).await? else {
        return Ok(());
    };
    let mut health = health.unwrap_or_default();
    let mut progress = FrameProgress::new(health.progress.as_ref());
    health.progress = Some(progress.health());
    let mut delay = 1;
    while !*stop.borrow() {
        health.updated_at_ms = now_ms();
        health.state = "connecting".into();
        match store.write_health(&config.camera_id, &health) {
            Ok(()) | Err(Error::Busy) => {},
            Err(error) => return Err(error),
        }
        let started = Instant::now();
        let result = attempt(
            &config,
            &store,
            &token,
            &mut stop,
            &mut health,
            &mut progress,
        )
        .await;
        if matches!(result, Err(Error::Cleanup | Error::Protection)) {
            health.state = "recording_error".into();
            health.updated_at_ms = now_ms();
            let _ = store.write_health(&config.camera_id, &health);
            return result;
        }
        if *stop.borrow() {
            break;
        }
        health.reconnects += 1;
        progress.source_retired();
        health.state = if result == Err(Error::Full) {
            "storage_full"
        } else {
            "reconnecting"
        }
        .into();
        health.updated_at_ms = now_ms();
        let _ = store.write_health(&config.camera_id, &health);
        eprintln!("nxrs-camera-activity: source retired; reconnecting");
        if started.elapsed() >= Duration::from_secs(30) {
            delay = 1;
        }
        tokio::select! { _ = stop.changed() => break, _ = tokio::time::sleep(Duration::from_secs(delay)) => {} }
        delay = (delay * 2).min(8);
        match store.prune(now_ms()) {
            Ok(()) | Err(Error::Busy) => {}
            Err(e) => return Err(e),
        }
    }
    health.updated_at_ms = now_ms();
    health.state = "stopped".into();
    let _ = store.write_health(&config.camera_id, &health);
    Ok(())
}

#[cfg(test)]
mod credential_tests {
    use super::*;
    fn config() -> Config {
        serde_json::from_value(serde_json::json!({
            "storage":{"directory":"/private/activity", "camera_ids":["cam"], "retention_seconds":3600,
                "max_bytes":67108864,"max_events":100},
            "camera_id":"cam","rtsp":"127.0.0.1:8554","stream_path":"one","username":"recorder",
            "reader":"/usr/local/libexec/nxrs-rtsp-reader","token_file":"/private/recorder.token","ffmpeg":"/usr/bin/ffmpeg","motion_percent":2,
            "pre_roll_seconds":2,"post_roll_seconds":4,"max_clip_seconds":10
        })).unwrap()
    }
    #[test]
    fn native_outputs_refuse_links_special_files_and_oversize() {
        use std::os::unix::{fs::symlink, net::UnixListener};
        let root = std::env::temp_dir().join(format!("n{}", &random_id().unwrap()[..8]));
        fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
        let secret = root.join("secret");
        fs::write(&secret, b"private-value").unwrap();
        let link = root.join("link");
        symlink(&secret, &link).unwrap();
        assert!(native_bytes(&link, 64).is_err());
        let hardlink = root.join("hardlink");
        fs::hard_link(&secret, &hardlink).unwrap();
        assert!(native_bytes(&hardlink, 64).is_err());
        fs::remove_file(hardlink).unwrap();
        assert!(native_bytes(&root, 64).is_err());
        let fifo = root.join("fifo");
        assert!(std::process::Command::new("/usr/bin/mkfifo")
            .args(["-m", "600"])
            .arg(&fifo)
            .status()
            .unwrap()
            .success());
        assert!(native_bytes(&fifo, 64).is_err());
        let media = root.join("media");
        fs::write(&media, b"media").unwrap();
        let mut opened = native_file(&media).unwrap();
        fs::remove_file(&media).unwrap();
        symlink(&secret, &media).unwrap();
        let mut bytes = Vec::new();
        opened.read_to_end(&mut bytes).unwrap();
        assert_eq!(bytes, b"media");
        assert!(native_bytes(&media, 64).is_err());

        let socket = root.join("socket");
        let listener = UnixListener::bind(&socket).unwrap();
        assert!(native_bytes(&socket, 64).is_err());
        drop(listener);
        assert_eq!(native_bytes(&secret, 2), Err(Error::Full));
        assert_eq!(native_bytes(&secret, 64).unwrap(), b"private-value");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn rejects_directive_url_and_credential_injection() {
        for bad in [
            "one\nfile /private/key",
            "one'",
            "one?query=secret",
            "../other",
            "one two",
            "one\r",
        ] {
            let mut c = config();
            c.stream_path = bad.into();
            assert!(c.validate().is_err());
            let mut c = config();
            c.username = bad.into();
            assert!(c.validate().is_err());
        }
        let mut c = config();
        c.rtsp = "192.168.1.1:8554".parse().unwrap();
        assert!(c.validate().is_err());
        let mut c = config();
        c.username = "u".repeat(64);
        c.stream_path = "s".repeat(64);
        assert!(c.validate().is_ok());
    }
}
