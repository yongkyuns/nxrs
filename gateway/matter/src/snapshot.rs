//! On-demand JPEG capture from the currently authorized H.264 publication.
use crate::{
    media::{Pictures, Video},
    privacy::{LiveView, Privacy},
};
use std::{
    io,
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    sync::{mpsc, oneshot, watch, OwnedSemaphorePermit, Semaphore},
    time,
};

pub const MAX_JPEG: usize = 48 * 1024;
pub const MAX_WIDTH: u16 = 640;
pub const MAX_HEIGHT: u16 = 360;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    Invalid,
    Busy,
    Unavailable,
    TooLarge,
    Encoder,
}

#[derive(Clone)]
pub struct Snapshots {
    video: Video,
    privacy: Privacy,
    requests: mpsc::Sender<Request>,
    slot: Arc<Semaphore>,
}

pub struct Worker {
    ffmpeg: PathBuf,
    requests: mpsc::Receiver<Request>,
}

struct Request {
    width: u16,
    height: u16,
    quality: u8,
    pictures: Pictures,
    view: LiveView,
    reply: oneshot::Sender<Result<Captured, Error>>,
    _slot: OwnedSemaphorePermit,
}

// Keep both guards through delivery to the command handler.
struct Captured {
    jpeg: Vec<u8>,
    pictures: Pictures,
    view: LiveView,
}

impl Snapshots {
    pub fn open(video: Video, privacy: Privacy, ffmpeg: &Path) -> Result<(Self, Worker), Error> {
        if !ffmpeg.is_absolute() || !ffmpeg.is_file() {
            return Err(Error::Invalid);
        }
        let (sender, receiver) = mpsc::channel(1);
        Ok((
            Self {
                video,
                privacy,
                requests: sender,
                slot: Arc::new(Semaphore::new(1)),
            },
            Worker {
                ffmpeg: ffmpeg.to_owned(),
                requests: receiver,
            },
        ))
    }

    pub async fn capture(&self, width: u16, height: u16, quality: u8) -> Result<Vec<u8>, Error> {
        if !valid_resolution(width, height) || !(1..=100).contains(&quality) {
            return Err(Error::Invalid);
        }
        let slot = self
            .slot
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::Busy)?;
        let pictures = self.video.subscribe_current().ok_or(Error::Unavailable)?;
        let view = self.privacy.live_view().ok_or(Error::Unavailable)?;
        let (reply, result) = oneshot::channel();
        self.requests
            .try_send(Request {
                width,
                height,
                quality,
                pictures,
                view,
                reply,
                _slot: slot,
            })
            .map_err(|_| Error::Unavailable)?;
        let image = result.await.map_err(|_| Error::Unavailable)??;
        if image.pictures.retired() || image.view.retired() {
            return Err(Error::Unavailable);
        }
        Ok(image.jpeg)
    }
}

pub fn valid_resolution(width: u16, height: u16) -> bool {
    (160..=MAX_WIDTH).contains(&width)
        && (120..=MAX_HEIGHT).contains(&height)
        && width.is_multiple_of(2)
        && height.is_multiple_of(2)
}

impl Worker {
    pub async fn run(mut self, mut stop: watch::Receiver<bool>) -> Result<(), Error> {
        let mut next_capture = time::Instant::now();
        while !*stop.borrow() {
            let request = tokio::select! {
                biased;
                _ = stop.changed() => break,
                request = self.requests.recv() => request,
            };
            let Some(mut request) = request else { break };
            // Forward only after the encoder exited or the cancellation path reaped it.
            let result = if time::Instant::now() < next_capture {
                Err(Error::Busy)
            } else {
                let result = self.capture(&mut request, &mut stop).await?;
                next_capture = time::Instant::now() + Duration::from_secs(1);
                result
            };
            let Request {
                reply,
                _slot,
                pictures,
                view,
                ..
            } = request;
            drop(_slot);
            let _ = reply.send(result.map(|jpeg| Captured {
                jpeg,
                pictures,
                view,
            }));
        }
        Ok(())
    }

    async fn capture(
        &self,
        req: &mut Request,
        stop: &mut watch::Receiver<bool>,
    ) -> Result<Result<Vec<u8>, Error>, Error> {
        let Request {
            pictures,
            view,
            reply,
            width,
            height,
            quality,
            ..
        } = req;
        // Even a quick off/on toggle changes the live privacy generation.
        let stop_on_privacy = async |view: &mut crate::privacy::LiveView| {
            while !view.retired() {
                view.changed().await;
            }
        };
        let picture = tokio::select! {
            biased;
            _ = stop.changed() => None,
            _ = reply.closed() => None,
            _ = stop_on_privacy(view) => None,
            result = time::timeout(Duration::from_secs(3), pictures.next()) => result.ok().flatten(),
        };
        let Some(picture) = picture else {
            return Ok(Err(Error::Unavailable));
        };
        if pictures.retired() || view.retired() || reply.is_closed() {
            return Ok(Err(Error::Unavailable));
        }

        let size = format!(
            "scale={width}:{height}:flags=bilinear:force_original_aspect_ratio=increase:force_divisible_by=2,crop={width}:{height}"
        );
        let quality = (2 + u16::from(100 - *quality) * 29 / 99).to_string();
        let mut child = match Command::new(&self.ffmpeg)
            .args([
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                "1",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-filter_threads",
                "1",
                "-vf",
                &size,
                "-frames:v",
                "1",
                "-c:v",
                "mjpeg",
                "-threads",
                "1",
                "-q:v",
                &quality,
                "-f",
                "image2pipe",
                "pipe:1",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
        {
            Ok(child) => child,
            Err(_) => return Ok(Err(Error::Encoder)),
        };

        let mut input = child.stdin.take().expect("piped stdin");
        let output = child.stdout.take().expect("piped stdout");
        let convert = async {
            let supply = async move {
                input.write_all(&picture.annex_b).await?;
                input.shutdown().await
            };
            let collect = async {
                let mut jpeg = Vec::new();
                output
                    .take(MAX_JPEG as u64 + 1)
                    .read_to_end(&mut jpeg)
                    .await?;
                Ok::<_, io::Error>(jpeg)
            };
            let (_, jpeg) = tokio::try_join!(supply, collect).map_err(|_| Error::Encoder)?;
            if jpeg.len() > MAX_JPEG {
                return Err(Error::TooLarge);
            }
            if !child.wait().await.map_err(|_| Error::Encoder)?.success()
                || !jpeg.starts_with(&[0xff, 0xd8])
                || !jpeg.ends_with(&[0xff, 0xd9])
            {
                return Err(Error::Encoder);
            }
            Ok(jpeg)
        };
        let result = tokio::select! {
            biased;
            _ = stop.changed() => Err(Error::Unavailable),
            _ = reply.closed() => Err(Error::Unavailable),
            _ = stop_on_privacy(view) => Err(Error::Unavailable),
            _ = pictures.wait_until_retired() => Err(Error::Unavailable),
            result = time::timeout(Duration::from_secs(2), convert) => result.unwrap_or(Err(Error::Encoder)),
        };
        if result.is_err() {
            // Await the barrier even after the caller cancels or the output cap trips.
            // A reap failure terminates the worker and closes future admission.
            let _ = child.start_kill();
            time::timeout(Duration::from_secs(2), child.wait())
                .await
                .map_err(|_| Error::Encoder)?
                .map_err(|_| Error::Encoder)?;
        }
        if pictures.retired() || view.retired() {
            return Ok(Err(Error::Unavailable));
        }
        Ok(result)
    }
}
