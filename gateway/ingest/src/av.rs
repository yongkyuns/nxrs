//! Timestamped input for one H.264/Opus encoder fed by the camera A/V reader.
mod matroska;

use crate::{
    config::Config,
    protocol::{Frame, Packet, Reader},
    runner, Error, Result,
};
use capture_wire::{AudioStamp, SAMPLE_RATE_HZ};
use std::time::Duration;
use tokio::{
    io::{AsyncRead, AsyncWrite},
    process::Command,
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
        "-f",
        "mpegts",
        "-mpegts_copyts",
        "1",
        "-muxdelay",
        "0",
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
    audio: Option<AudioStamp>,
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
            audio: None,
            timeline: Timeline {
                window_ms: config.frame_timeout_ms,
                ..Timeline::default()
            },
        })
    }

    /// Read and write one complete camera part. `None` means audio advanced.
    /// On error the caller must retire the encoder and discard the connection.
    pub async fn next(&mut self) -> Result<Option<Picture>> {
        let deadline = self.deadlines[0].min(self.deadlines[1]);
        if Instant::now() >= deadline {
            return Err(Error::Timeout);
        }
        let (track, picture) = timeout_at(deadline, async {
            match self.source.next_packet().await? {
                Packet::Video(frame) => {
                    let track = Track::Video;
                    let pts_ms = self
                        .timeline
                        .pts(track, frame.capture_ms, frame.capture_ms)?;
                    self.output
                        .write(track, pts_ms, self.source.payload(frame))
                        .await?;
                    Ok::<_, Error>((track, Some(Picture { frame, pts_ms })))
                }
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
                    self.output.write(track, pts, block.pcm).await?;
                    Ok((track, None))
                }
            }
        })
        .await
        .map_err(|_| Error::Timeout)??;
        let finished = Instant::now();
        if finished >= deadline {
            return Err(Error::Timeout);
        }
        self.deadlines[track as usize] = finished + self.budget;
        Ok(picture)
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
        if pts < 20
            || pts > i64::MAX as u64 / 1000
            || self.track_pts[index].is_some_and(|last| pts <= last)
            || self.track_pts[1 - index].is_some_and(|other| pts.abs_diff(other) > self.window_ms)
        {
            return Err(Error::Stale);
        }
        self.track_pts[index] = Some(pts);
        Ok(pts)
    }
}
