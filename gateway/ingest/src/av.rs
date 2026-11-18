//! Timestamped input for one H.264/Opus encoder fed by the camera A/V reader.
mod matroska;

use crate::{
    config::Config,
    detections::Sample,
    protocol::{Frame, Packet, Reader},
    runner, Error, Result,
};
use capture_wire::{AudioStamp, MicrophoneState, BLOCK_BYTES, BLOCK_SAMPLES, SAMPLE_RATE_HZ};
use detection_wire::{Version, VideoFrame};
use serde::Deserialize;
use std::time::Duration;
use tokio::{
    io::{AsyncRead, AsyncWrite},
    process::Command,
    sync::watch,
    time::{timeout_at, Instant},
};

pub fn encoder(config: &Config) -> Command {
    let mut command = runner::ffmpeg(config);
    command.args([
        "-copyts",
        "-f",
        "matroska",
        "-probesize",
        "32",
        "-analyzeduration",
        "1",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-map",
        "0:s:0",
        "-enc_time_base:v",
        "1:1000",
    ]);
    runner::h264(&mut command, "passthrough");
    // Match the JPEG encoder's nominal rate for x264 level selection. The
    // Matroska millisecond clock still carries the original capture timestamps.
    command.args(["-x264-params", "fps=30/1"]);
    command.args([
        "-c:a",
        "libopus",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-b:a",
        "64k",
        "-frame_duration",
        "20",
        "-c:s",
        "copy",
        "-f",
        "matroska",
        "-live",
        "1",
        "-cluster_time_limit",
        "40",
        "-avoid_negative_ts",
        "disabled",
        "-max_interleave_delta",
        "250000",
        "-flush_packets",
        "1",
        "pipe:1",
    ]);
    command
}

#[derive(Clone, Copy)]
enum Track {
    Video = 0,
    Audio = 1,
}

const BLOCK_MS: u64 = 1000 * BLOCK_SAMPLES / SAMPLE_RATE_HZ as u64;
const SILENT_PCM: [u8; BLOCK_BYTES] = [0; BLOCK_BYTES];

/// A validated JPEG and its encoder input timestamp in milliseconds.
#[derive(Debug)]
pub struct Picture {
    pub frame: Frame,
    pub pts_ms: u64,
}

pub struct EncoderInput<R, W> {
    source: Reader<R>,
    output: matroska::Writer<W>,
    budget: Duration,
    deadlines: [Instant; 2],
    microphone_deadline: Option<Instant>,
    audio: Option<AudioStamp>,
    silence_until: Option<u64>,
    epoch: String,
    timeline: Timeline,
}

impl<R: AsyncRead + Unpin, W: AsyncWrite + Unpin> EncoderInput<R, W> {
    /// The caller authorizes the camera transport before negotiating A/V.
    pub async fn start(source: Reader<R>, output: W, config: &Config) -> Result<Self> {
        config.validate()?;
        if source.av_dimensions() != Some((config.width, config.height)) {
            return Err(Error::Protocol);
        }
        let budget = Duration::from_millis(config.frame_timeout_ms);
        let deadline = Instant::now() + budget;
        let output = timeout_at(
            deadline,
            matroska::Writer::start(output, config.width, config.height),
        )
        .await
        .map_err(|_| Error::Timeout)??;
        Ok(Self {
            source,
            output,
            budget,
            deadlines: [deadline; 2],
            microphone_deadline: None,
            audio: None,
            silence_until: None,
            epoch: runner::epoch()?,
            timeline: Timeline {
                window_ms: config.frame_timeout_ms,
                ..Timeline::default()
            },
        })
    }

    /// Send a command between complete camera parts. A mute request withholds
    /// PCM immediately, including the last in-flight block before the ACK.
    /// Retire this input on a failed write or a missing acknowledgement.
    pub async fn request_microphone(&mut self, muted: bool) -> Result<MicrophoneState>
    where
        R: AsyncWrite,
    {
        let deadline = Instant::now() + self.budget;
        let state = timeout_at(deadline, self.source.request_microphone(muted))
            .await
            .map_err(|_| Error::Timeout)??;
        self.microphone_deadline = Some(deadline);
        Ok(state)
    }

    /// Read a complete camera part and return a picture only for video.
    /// On error the caller must retire the encoder and discard the connection.
    pub async fn next(&mut self) -> Result<Option<Picture>> {
        self.next_admitted(|| true).await
    }

    async fn next_admitted(&mut self, permitted: impl FnOnce() -> bool) -> Result<Option<Picture>> {
        let deadline = self.deadlines[0].min(self.deadlines[1]);
        let deadline = self
            .microphone_deadline
            .map_or(deadline, |ack| deadline.min(ack));
        if Instant::now() >= deadline {
            return Err(Error::Timeout);
        }
        let muted_at_start = self.source.microphone_muted();
        let (picture, audio_written) = timeout_at(deadline, async {
            let packet = self.source.next_packet().await?;
            let muted = muted_at_start || !permitted();
            match packet {
                Packet::Video(frame) => {
                    let track = Track::Video;
                    let pts_ms = self
                        .timeline
                        .pts(track, frame.capture_ms, frame.capture_ms)?;
                    self.timeline.place(track, pts_ms)?;
                    let annotation = VideoFrame {
                        api_version: Version::V1,
                        epoch: self.epoch.clone(),
                        frame_sequence: frame.sequence,
                        frame_capture_ms: frame.capture_ms,
                        source: self.source.metadata.clone(),
                    }
                    .encode()
                    .map_err(|_| Error::Protocol)?;
                    self.output.write_annotation(pts_ms, &annotation).await?;
                    self.output
                        .write(track, pts_ms, self.source.payload(frame))
                        .await?;
                    let audio_written = muted && self.write_silence(pts_ms).await?;
                    Ok::<_, Error>((Some(Picture { frame, pts_ms }), audio_written))
                }
                Packet::Audio(_) if muted => Ok((None, false)),
                Packet::Audio(block) => {
                    let track = Track::Audio;
                    let stamp = block.stamp;
                    let anchor = *self.audio.get_or_insert(stamp);
                    let samples = stamp.first_sample - anchor.first_sample;
                    let sample_time = anchor
                        .capture_ms
                        .checked_add(samples / u64::from(SAMPLE_RATE_HZ / 1000))
                        .ok_or(Error::Stale)?;
                    let pts = self.timeline.pts(track, sample_time, stamp.capture_ms)?;
                    if self.silence_until.is_some_and(|end| pts < end) {
                        return Ok((None, false));
                    }
                    self.timeline.place(track, pts)?;
                    self.output.write(track, pts, block.pcm).await?;
                    self.silence_until = None;
                    Ok((None, true))
                }
                Packet::Microphone { changed, .. } => {
                    self.microphone_deadline = None;
                    if changed {
                        self.audio = None;
                        self.timeline.lag[Track::Audio as usize] = runner::Lag::default();
                    }
                    Ok((None, false))
                }
            }
        })
        .await
        .map_err(|_| Error::Timeout)??;
        let finished = Instant::now();
        if finished >= deadline {
            return Err(Error::Timeout);
        }
        if picture.is_some() {
            self.deadlines[Track::Video as usize] = finished + self.budget;
        }
        if audio_written {
            self.deadlines[Track::Audio as usize] = finished + self.budget;
        }
        Ok(picture)
    }

    async fn write_silence(&mut self, video_pts: u64) -> Result<bool> {
        // Advance only through a fresh picture's capture time. Muted silence
        // never replaces video progress or hides a stalled camera connection.
        let track = Track::Audio;
        let start = self.timeline.track_pts[track as usize]
            .map_or(video_pts - BLOCK_MS, |last| last + BLOCK_MS);
        let mut pts = start;
        while pts + BLOCK_MS <= video_pts {
            self.timeline.place(track, pts)?;
            self.output.write(track, pts, &SILENT_PCM).await?;
            pts += BLOCK_MS;
        }
        if pts != start {
            self.silence_until = Some(pts);
        }
        Ok(pts != start)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Permit {
    id: [u8; 16],
    valid_for_ms: u64,
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct CapturePermission {
    id: [u8; 16],
    deadline: Instant,
}

fn current(receiver: &watch::Receiver<Option<CapturePermission>>) -> Option<CapturePermission> {
    receiver
        .borrow()
        .filter(|value| value.deadline > Instant::now())
}

pub(crate) async fn pump<R: AsyncRead + AsyncWrite + Unpin, W: AsyncWrite + Unpin>(
    source: Reader<R>,
    output: W,
    config: &Config,
    metadata: watch::Sender<Option<Sample>>,
    publisher_token: &str,
) -> Result<()> {
    let control = config.microphone_control.ok_or(Error::Configuration)?;
    let enrollment = config.camera_tls.as_ref().ok_or(Error::Configuration)?;
    let (permits, permit) = watch::channel(None);
    let poll = async {
        loop {
            let requested = Instant::now();
            let body = crate::control::publisher_post(
                control,
                &enrollment.camera_id,
                &config.publisher_user,
                publisher_token,
                "microphone-permit",
                b"{}",
            )
            .await;
            let received = body
                .ok()
                .and_then(|body| serde_json::from_slice::<Option<Permit>>(&body).ok())
                .flatten()
                .filter(|permit| (1..=1500).contains(&permit.valid_for_ms))
                .map(|permit| CapturePermission {
                    id: permit.id,
                    // Time in the request/response already consumes the lease.
                    deadline: requested + Duration::from_millis(permit.valid_for_ms),
                })
                .filter(|permission| permission.deadline > Instant::now());
            permits.send_if_modified(|current| {
                let changed = *current != received;
                *current = received;
                changed
            });
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    };
    let encode = pump_with_permits(source, output, config, metadata, permit);
    tokio::select! { result = encode => result, () = poll => Err(Error::Stopped) }
}

async fn pump_with_permits<R: AsyncRead + AsyncWrite + Unpin, W: AsyncWrite + Unpin>(
    source: Reader<R>,
    output: W,
    config: &Config,
    metadata: watch::Sender<Option<Sample>>,
    permit: watch::Receiver<Option<CapturePermission>>,
) -> Result<()> {
    let mut input = EncoderInput::start(source, output, config).await?;
    let mut authorized = None;
    loop {
        let granted = current(&permit).map(|permission| permission.id);
        if authorized.is_some() && granted != authorized {
            if input.microphone_deadline.is_some() {
                // The pending unmute is superseded. Closing transport cancels
                // capture without waiting for it to start.
                return Err(Error::Stale);
            }
            authorized = None;
            input.request_microphone(true).await?;
        } else if authorized.is_none() && granted.is_some() && input.microphone_deadline.is_none() {
            authorized = granted;
            input.request_microphone(false).await?;
        }
        let part =
            input.next_admitted(|| current(&permit).is_some_and(|p| Some(p.id) == authorized));
        let retire_stalled = async {
            if authorized.is_none() {
                std::future::pending::<()>().await;
            }
            let mut changes = permit.clone();
            loop {
                let permission = *changes.borrow_and_update();
                let Some(permission) =
                    permission.filter(|p| Some(p.id) == authorized && p.deadline > Instant::now())
                else {
                    break;
                };
                tokio::select! {
                    _ = tokio::time::sleep_until(permission.deadline) => {},
                    change = changes.changed() => if change.is_err() { break; },
                }
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        };
        let picture = tokio::select! {
            biased;
            _ = retire_stalled => return Err(Error::Stale),
            result = part => result?,
        };
        if let Some(picture) = picture {
            if let Some(sample) = &input.source.metadata {
                metadata.send_replace(Some(Sample {
                    frame: picture.frame,
                    source: sample.clone(),
                    received: Instant::now(),
                }));
            }
        }
    }
}

#[derive(Default)]
struct Timeline {
    window_ms: u64,
    lag: [runner::Lag; 2],
    first_capture: Option<u64>,
    track_pts: [Option<u64>; 2],
}

impl Timeline {
    fn pts(&mut self, track: Track, sample_ms: u64, observed_ms: u64) -> Result<u64> {
        let index = track as usize;
        self.lag[index].observe(observed_ms, Instant::now())?;
        if sample_ms.abs_diff(observed_ms) > self.window_ms {
            return Err(Error::Stale);
        }
        let origin = *self.first_capture.get_or_insert(sample_ms);
        // Leave room for the other track to start slightly earlier, plus the
        // Opus encoder's first 20 ms frame and priming delay.
        let relative = i128::from(sample_ms) - i128::from(origin) + i128::from(self.window_ms) + 20;
        let pts = u64::try_from(relative).map_err(|_| Error::Stale)?;
        if pts < BLOCK_MS || pts > i64::MAX as u64 / 1000 {
            return Err(Error::Stale);
        }
        Ok(pts)
    }

    fn place(&mut self, track: Track, pts: u64) -> Result<()> {
        let index = track as usize;
        if self.track_pts[index].is_some_and(|last| pts <= last)
            || self.track_pts[1 - index].is_some_and(|other| pts.abs_diff(other) > self.window_ms)
        {
            return Err(Error::Stale);
        }
        self.track_pts[index] = Some(pts);
        Ok(())
    }
}
