//! One authenticated loopback RTSP reader. Original H.264 is remuxed; 96x72 gray
//! samples are decoded at 2 Hz for motion. There is never another camera connection.
use crate::{
    identifier,
    motion::{Detector, PIXELS},
    now_ms,
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
    io::{AsyncBufReadExt, AsyncReadExt, BufReader},
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
        Ok(config)
    }
    fn token(&self) -> Result<String, Error> {
        let bytes = private_read(&self.token_file, 128)?;
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
        Ok(token.into())
    }
}
fn private_read(path: &Path, max: u64) -> Result<Vec<u8>, Error> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
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
fn random_id() -> Result<String, Error> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| Error::Io)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn command(ffmpeg: &Path) -> Command {
    let mut c = Command::new(ffmpeg);
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
    c
}
async fn reap(child: &mut Child) -> Result<(), Error> {
    if child.try_wait()?.is_some() {
        return Ok(());
    }
    child.start_kill()?;
    timeout(Duration::from_secs(3), child.wait())
        .await
        .map_err(|_| Error::Io)??;
    Ok(())
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
            let meta = fs::symlink_metadata(&segment.path)?;
            if !meta.is_file() || meta.file_type().is_symlink() {
                return Err(Error::Corrupt);
            }
            total += meta.len();
            if total > MAX_CLIP_BYTES {
                return Err(Error::Full);
            }
            let name = format!("part-{i:03}.ts");
            fs::copy(&segment.path, job.join(&name))?;
            list.push_str(&format!("file '{name}'\n"));
        }
        fs::write(job.join("inputs.txt"), list)?;
        let clip = job.join("clip.mp4");
        let image = job.join("thumbnail.jpg");
        let mut child = command(&config.ffmpeg)
            .args(["-f", "concat", "-safe", "1", "-i"])
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
        let bytes = fs::metadata(&clip)?.len();
        let thumb_bytes = fs::metadata(&image)?.len();
        if !(1..MAX_CLIP_BYTES).contains(&bytes)
            || !(1..=MAX_THUMBNAIL_BYTES).contains(&thumb_bytes)
        {
            return Err(Error::Full);
        }
        let clip = fs::read(clip)?;
        let image = fs::read(image)?;
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
    let _ = fs::remove_dir_all(job);
    result
}

async fn attempt(
    config: &Config,
    store: &Store,
    token: &str,
    stop: &mut watch::Receiver<bool>,
    health: &mut Health,
) -> Result<(), Error> {
    let directory = config.storage.directory.join("spool").join(format!(
        "source-{}-{}",
        config.camera_id,
        random_id()?
    ));
    fs::DirBuilder::new().mode(0o700).create(&directory)?;
    let mut child = command(&config.ffmpeg)
        .args([
            "-rtsp_transport",
            "tcp",
            "-timeout",
            "5000000",
            "-probesize",
            "32768",
            "-analyzeduration",
            "1000000",
            "-i",
        ])
        .arg(format!(
            "rtsp://{}:{}@{}/{}",
            config.username, token, config.rtsp, config.stream_path
        ))
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
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let (sender, mut input) = mpsc::channel(8);
    let a = child.stdout.take().ok_or(Error::Io)?;
    let b = child.stderr.take().ok_or(Error::Io)?;
    let mut segment_reader = tokio::spawn(segments(a, directory.clone(), sender.clone()));
    let mut gray_reader = tokio::spawn(gray_frames(b, sender));
    let mut segments_finished = false;
    let mut gray_finished = false;
    let mut detector = Detector::new(config.motion_percent).map_err(|_| Error::Invalid)?;
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
                _ = child.wait() => break Err(Error::Io),
                _ = &mut segment_reader => { segments_finished = true; break Err(Error::Io); },
                _ = &mut gray_reader => { gray_finished = true; break Err(Error::Io); },
                event = input.recv() => match event.ok_or(Error::Io)? {
                    Input::Gray(gray) => {
                        last_frame = Instant::now(); seen_frame = true; health.frames_observed += 1; health.last_frame_at_ms = Some(now_ms());
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
                        let entry = entry?; count += 1; bytes += entry.metadata()?.len();
                    }
                    if count > 40 || bytes > 64 * 1024 * 1024 { return Err(Error::Full); }
                    if job.as_ref().is_some_and(|j| j.is_finished()) {
                        match job.take().ok_or(Error::Io)?.await.map_err(|_| Error::Io)? {
                            Ok(()) => { health.clips_created += 1; health.state = "monitoring".into(); },
                            Err(e) => { health.clips_dropped += 1; health.state = if e == Error::Full { "storage_full" } else { "recording_error" }.into(); },
                        }
                        pinned.clear();
                    }
                    if last_health.elapsed() >= Duration::from_secs(5) {
                        health.updated_at_ms = now_ms();
                        if !matches!(health.state.as_str(), "storage_full" | "recording_error") { health.state = "monitoring".into(); }
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
    // Reap the single source reader before reconnect; never overlap observer generations.
    let reaped = reap(&mut child).await;
    if !segments_finished {
        segment_reader.abort();
        let _ = segment_reader.await;
    }
    if !gray_finished {
        gray_reader.abort();
        let _ = gray_reader.await;
    }
    if let Some(job) = job {
        match job.await {
            Ok(Ok(())) => health.clips_created += 1,
            _ => health.clips_dropped += 1,
        }
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
                Err(_) => health.clips_dropped += 1,
            }
        }
    }
    let _ = fs::remove_dir_all(directory);
    reaped?;
    result
}

pub async fn run(config: Config, mut stop: watch::Receiver<bool>) -> Result<(), Error> {
    config.validate()?;
    let token = config.token()?;
    let store = Store::open(config.storage.clone())?;
    // Exclusive lifetime lease: a second observer cannot compete for the same camera.
    let lease = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(
            config
                .storage
                .directory
                .join(format!("worker-{}.lock", config.camera_id)),
        )?;
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
    let mut health = store.health(&config.camera_id)?.unwrap_or_default();
    let mut delay = 1;
    while !*stop.borrow() {
        health.updated_at_ms = now_ms();
        health.state = "connecting".into();
        store.write_health(&config.camera_id, &health)?;
        let started = Instant::now();
        let result = attempt(&config, &store, &token, &mut stop, &mut health).await;
        if *stop.borrow() {
            break;
        }
        health.reconnects += 1;
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
