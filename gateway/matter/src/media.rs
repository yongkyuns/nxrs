//! A single authenticated RTSP/TCP input distributes encoded camera access units.
use bytes::Bytes;
use futures_util::StreamExt;
use retina::{
    client::{Credentials, Session, SessionOptions, SetupOptions, TeardownPolicy, Transport},
    codec::{CodecItem, FrameFormat, ParametersRef},
};
use std::{
    net::SocketAddr,
    path::Path,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::{
    sync::broadcast,
    time::{self, Instant},
};
use url::Url;

mod paired;
mod silence;

const STARTUP: Duration = Duration::from_secs(10);
const SILENCE: Duration = Duration::from_secs(5);
const MAX_UNIT: usize = 1_048_576;
const QUEUE: usize = 8;

/// One encoded picture. The 90 kHz timestamps share a clock within a source session.
#[derive(Clone)]
pub struct AccessUnit {
    pub pts_90khz: u64,
    /// Source time mapped to the gateway clock. Video-only sources use receipt time.
    pub source_time: std::time::Instant,
    pub keyframe: bool,
    pub dimensions: (u32, u32),
    pub annex_b: Bytes,
}

#[derive(Clone)]
enum Message {
    Picture(u64, AccessUnit),
    End,
}

/// Each peer subscribes independently. A slow peer cannot hold up the input.
#[derive(Clone)]
pub struct Video {
    sender: broadcast::WeakSender<Message>,
    sound: broadcast::WeakSender<Option<(u64, AudioPacket)>>,
    current: Arc<AtomicU64>,
    opus_generation: Arc<AtomicU64>,
}

impl Video {
    /// Listen to the current authorized microphone. Replacement retires this subscription.
    pub fn subscribe_audio(&self) -> Option<Audio> {
        let queue = self.sound.upgrade()?.subscribe();
        let generation = self.current.load(Ordering::Acquire);
        (generation != 0 && self.opus_generation.load(Ordering::Acquire) == generation).then(|| {
            Audio {
                queue,
                generation,
                current: self.current.clone(),
            }
        })
    }

    /// Both tracks of the current authorized source have supplied timed media.
    pub fn has_opus_track(&self) -> bool {
        let generation = self.current.load(Ordering::Acquire);
        generation != 0 && generation == self.opus_generation.load(Ordering::Acquire)
    }

    pub fn subscribe(&self) -> Option<Pictures> {
        Some(Pictures {
            queue: self.sender.upgrade()?.subscribe(),
            current: self.current.clone(),
            generation: None,
            need_keyframe: true,
            deadline: None,
        })
    }
}

#[derive(Clone)]
pub struct AudioPacket {
    pub pts_48khz: u64,
    pub source_time: std::time::Instant,
    pub opus: Bytes,
}

pub struct Audio {
    queue: broadcast::Receiver<Option<(u64, AudioPacket)>>,
    generation: u64,
    current: Arc<AtomicU64>,
}

impl Audio {
    pub fn discard_queued(&mut self) {
        self.queue = self.queue.resubscribe();
    }
    pub fn is_current(&self) -> bool {
        self.generation == self.current.load(Ordering::Acquire)
    }

    pub async fn next(&mut self) -> Option<AudioPacket> {
        let deadline = Instant::now() + SILENCE;
        loop {
            if !self.is_current() {
                return None;
            }
            match time::timeout_at(deadline, self.queue.recv()).await.ok()? {
                Ok(Some((g, p)))
                    if g == self.generation && g == self.current.load(Ordering::Acquire) =>
                {
                    return Some(p)
                }
                Err(broadcast::error::RecvError::Lagged(_)) => {
                    self.queue = self.queue.resubscribe()
                }
                _ => return None,
            }
        }
    }
}

pub struct Pictures {
    queue: broadcast::Receiver<Message>,
    current: Arc<AtomicU64>,
    generation: Option<u64>,
    need_keyframe: bool,
    deadline: Option<Instant>,
}

impl Pictures {
    /// A peer starts or recovers from lag at a keyframe. A retired source ends
    /// its peers so a replacement cannot inherit the original RTP clock.
    pub async fn next(&mut self) -> Option<AccessUnit> {
        loop {
            if self
                .generation
                .is_some_and(|g| g != self.current.load(Ordering::Acquire))
            {
                return None;
            }
            let deadline = *self
                .deadline
                .get_or_insert_with(|| Instant::now() + STARTUP);
            if Instant::now() >= deadline {
                return None;
            }
            match time::timeout_at(deadline, self.queue.recv()).await.ok()? {
                Ok(Message::Picture(g, frame)) => {
                    if g != self.current.load(Ordering::Acquire) {
                        continue;
                    }
                    if self.generation.is_some_and(|old| old != g) {
                        return None;
                    }
                    if self.need_keyframe && !frame.keyframe {
                        continue;
                    }
                    self.need_keyframe = false;
                    self.generation = Some(g);
                    self.deadline = Some(Instant::now() + SILENCE);
                    return Some(frame);
                }
                Ok(Message::End) if self.generation.is_some() => return None,
                Ok(Message::End) => {}
                Err(broadcast::error::RecvError::Lagged(_)) => {
                    self.queue = self.queue.resubscribe();
                    self.need_keyframe = true;
                }
                Err(broadcast::error::RecvError::Closed) => return None,
            }
        }
    }
}

pub struct Reader {
    url: Url,
    credentials: Credentials,
    sender: broadcast::Sender<Message>,
    sound: broadcast::Sender<Option<(u64, AudioPacket)>>,
    current: Arc<AtomicU64>,
    opus_generation: Arc<AtomicU64>,
}

impl Reader {
    pub fn open(
        address: SocketAddr,
        path: &str,
        user: &str,
        token_file: &Path,
    ) -> Result<(Self, Video), &'static str> {
        if !address.ip().is_loopback()
            || address.port() == 0
            || !crate::credential::identifier(path)
            || !crate::credential::identifier(user)
        {
            return Err("invalid local media input");
        }
        let url = Url::parse(&format!("rtsp://{address}/{path}"))
            .map_err(|_| "invalid local media input")?;
        let credentials = Credentials {
            username: user.into(),
            password: crate::credential::read(token_file)?,
        };
        let sender = broadcast::Sender::new(QUEUE);
        let sound = broadcast::Sender::new(16);
        let current = Arc::new(AtomicU64::new(0));
        let opus_generation = Arc::new(AtomicU64::new(0));
        let video = Video {
            sound: sound.downgrade(),
            sender: sender.downgrade(),
            opus_generation: opus_generation.clone(),
            current: current.clone(),
        };
        Ok((
            Self {
                sound,
                url,
                credentials,
                opus_generation,
                sender,
                current,
            },
            video,
        ))
    }

    /// Own the input on the caller's executor. Dropping this future closes the
    /// interleaved TCP session; no reconnect or teardown task outlives the owner.
    pub async fn run(self) {
        for generation in 1..=u64::MAX {
            self.current.store(generation, Ordering::Release);
            tokio::select! {
                biased;
                _ = self.check_permission() => {},
                _ = self.receive(generation) => {},
            }
            self.retire();
            time::sleep(Duration::from_secs(1)).await;
        }
    }

    fn retire(&self) {
        self.current.store(0, Ordering::Release);
        let _ = self.sender.send(Message::End);
        let _ = self.sound.send(None);
    }

    async fn describe(&self) -> Result<Session<retina::client::Described>, ()> {
        let options = SessionOptions::default()
            .creds(Some(self.credentials.clone()))
            .teardown(TeardownPolicy::Never);
        Session::describe(self.url.clone(), options)
            .await
            .map_err(|_| ())
    }

    // MediaMTX authenticates DESCRIBE through the same path-scoped HTTP policy
    // as SETUP. DESCRIBE alone opens no media reader. Run it alongside packet
    // delivery so a dead controller cannot leave an established stream open.
    async fn check_permission(&self) {
        loop {
            time::sleep(Duration::from_secs(3)).await;
            if !matches!(
                time::timeout(Duration::from_secs(2), self.describe()).await,
                Ok(Ok(_))
            ) {
                return;
            }
        }
    }

    async fn receive(&self, generation: u64) -> Result<(), ()> {
        let opening = async {
            let mut session = self.describe().await?;
            let mut videos = session
                .streams()
                .iter()
                .enumerate()
                .filter(|(_, s)| s.media() == "video");
            let (index, video) = videos.next().ok_or(())?;
            if videos.next().is_some()
                || video.encoding_name() != "h264"
                || video.clock_rate_hz() != 90_000
            {
                return Err(());
            }
            let mut audio = session
                .streams()
                .iter()
                .enumerate()
                .filter(|(_, s)| s.media() == "audio");
            let opus = audio
                .next()
                .and_then(|(id, s)| {
                    (s.encoding_name() == "opus" && s.clock_rate_hz() == 48_000).then_some(id)
                })
                .filter(|_| audio.next().is_none());
            let setup = SetupOptions::default()
                .transport(Transport::Tcp(Default::default()))
                .frame_format(FrameFormat::SIMPLE);
            session.setup(index, setup).await.map_err(|_| ())?;
            if let Some(id) = opus {
                session
                    .setup(
                        id,
                        SetupOptions::default().transport(Transport::Tcp(Default::default())),
                    )
                    .await
                    .map_err(|_| ())?;
            }
            // Per-track NPT zero is unrelated across tracks. Paired input uses
            // original RTP timestamps and RTCP sender reports exclusively.
            let play = retina::client::PlayOptions::default()
                .initial_timestamp(retina::client::InitialTimestampPolicy::Ignore);
            let media = session.play(play).await.map_err(|_| ())?;
            Ok((media, index, opus))
        };
        let (media, track, opus) = time::timeout(STARTUP, opening).await.map_err(|_| ())??;
        if let Some(audio) = opus {
            return paired::receive(self, generation, media, track, audio).await;
        }
        let mut media = media.demuxed().map_err(|_| ())?;
        let mut deadline = Instant::now() + SILENCE;
        let mut first = None;
        let mut previous = None;
        loop {
            let item = time::timeout_at(deadline, media.next())
                .await
                .map_err(|_| ())?
                .ok_or(())?
                .map_err(|_| ())?;
            let CodecItem::VideoFrame(frame) = item else {
                continue;
            };
            if frame.stream_id() != track
                || frame.loss() != 0
                || !(1..=MAX_UNIT).contains(&frame.data().len())
            {
                return Err(());
            }
            if first.is_none() && !frame.is_random_access_point() {
                continue;
            }
            if first.is_some() && frame.has_new_parameters() {
                return Err(());
            }
            let Some(ParametersRef::Video(params)) = media.streams()[track].parameters() else {
                return Err(());
            };
            let (w, h) = params.pixel_dimensions();
            if !(2..=1920).contains(&w) || !(2..=1080).contains(&h) {
                return Err(());
            }
            let tick = frame.timestamp().timestamp();
            if previous.is_some_and(|p| tick <= p) {
                return Err(());
            }
            previous = Some(tick);
            let pts_90khz = u64::try_from(tick.checked_sub(*first.get_or_insert(tick)).ok_or(())?)
                .map_err(|_| ())?;
            let unit = AccessUnit {
                pts_90khz,
                source_time: std::time::Instant::now(),
                keyframe: frame.is_random_access_point(),
                dimensions: (w, h),
                annex_b: frame.into_data().into(),
            };
            let _ = self.sender.send(Message::Picture(generation, unit));
            deadline = Instant::now() + SILENCE;
        }
    }
}

impl Drop for Reader {
    fn drop(&mut self) {
        self.retire();
    }
}

#[cfg(test)]
mod tests;
