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
        let deadline = self.deadlines[0].min(self.deadlines[1]);
        let deadline = self
            .microphone_deadline
            .map_or(deadline, |ack| deadline.min(ack));
        if Instant::now() >= deadline {
            return Err(Error::Timeout);
        }
        let muted = self.source.microphone_muted();
        let (picture, audio_written) = timeout_at(deadline, async {
            match self.source.next_packet().await? {
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

pub(crate) async fn pump<R: AsyncRead + AsyncWrite + Unpin, W: AsyncWrite + Unpin>(
    source: Reader<R>,
    output: W,
    config: &Config,
    metadata: watch::Sender<Option<Sample>>,
) -> Result<()> {
    let mut input = EncoderInput::start(source, output, config).await?;
    // Enabling microphone capture is currently an explicit ingest setting.
    input.request_microphone(false).await?;
    loop {
        if let Some(picture) = input.next().await? {
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
