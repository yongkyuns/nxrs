//! One private directory, an interprocess std file lock and atomic event-directory publication.
//! Only unkept automatic events expire. Quota pressure never evicts kept footage.
use crate::{event_id, identifier};
use serde::{Deserialize, Serialize};
#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Component, Path, PathBuf},
};

pub const MAX_CLIP_BYTES: u64 = 32 * 1024 * 1024;
pub const MAX_THUMBNAIL_BYTES: u64 = 256 * 1024;
const MAX_EVENTS: usize = 2000;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    #[serde(default)]
    pub protection: Option<crate::protection::Volume>,
    pub directory: PathBuf,
    pub camera_ids: Vec<String>,
    pub retention_seconds: u64,
    pub max_bytes: u64,
    pub max_events: usize,
}
impl Settings {
    pub fn verify_protection(&self) -> Result<(), Error> {
        if let Some(volume) = &self.protection {
            volume.verify(&self.directory)?;
        }
        Ok(())
    }
    pub fn validate(&self) -> Result<(), Error> {
        if !self.directory.is_absolute()
            || self
                .directory
                .components()
                .any(|c| matches!(c, Component::ParentDir))
            || self.camera_ids.is_empty()
            || self.camera_ids.len() > 64
            || self.camera_ids.iter().any(|v| !identifier(v))
            || self
                .camera_ids
                .iter()
                .collect::<std::collections::HashSet<_>>()
                .len()
                != self.camera_ids.len()
            || !(60..=30 * 86400).contains(&self.retention_seconds)
            || !(MAX_CLIP_BYTES..=100 * 1024 * 1024 * 1024).contains(&self.max_bytes)
            || !(1..=MAX_EVENTS).contains(&self.max_events)
        {
            return Err(Error::Invalid);
        }
        if let Some(volume) = &self.protection {
            volume.validate(&self.directory)?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    Invalid,
    Io,
    Busy,
    Full,
    NotFound,
    Corrupt,
    Cleanup,
    Protection,
}
impl From<std::io::Error> for Error {
    fn from(_: std::io::Error) -> Self {
        Self::Io
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Invalid => "invalid activity configuration",
            Self::Io => "activity storage unavailable",
            Self::Busy => "activity storage busy",
            Self::Full => "activity storage full",
            Self::NotFound => "activity not found",
            Self::Corrupt => "activity index needs attention",
            Self::Cleanup => "activity process cleanup incomplete",
            Self::Protection => "encrypted activity storage unavailable",
        })
    }
}
impl std::error::Error for Error {}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub event_id: String,
    pub camera_id: String,
    pub kind: String,
    /// Gateway-observed UTC time; never described as sensor exposure time.
    pub started_at_ms: u64,
    pub ended_at_ms: u64,
    pub clip_bytes: u64,
    pub thumbnail_bytes: u64,
    pub kept: bool,
    pub interrupted: bool,
}
impl Event {
    fn validate(&self) -> Result<(), Error> {
        if !event_id(&self.event_id)
            || !identifier(&self.camera_id)
            || self.kind != "motion"
            || self.started_at_ms == 0
            || self.ended_at_ms <= self.started_at_ms
            || self.ended_at_ms - self.started_at_ms > 120_000
            || !(1..=MAX_CLIP_BYTES).contains(&self.clip_bytes)
            || !(1..=MAX_THUMBNAIL_BYTES).contains(&self.thumbnail_bytes)
        {
            return Err(Error::Corrupt);
        }
        Ok(())
    }
    fn bytes(&self) -> u64 {
        self.clip_bytes + self.thumbnail_bytes
    }
}

#[derive(Serialize)]
pub struct Page {
    pub events: Vec<Event>,
    pub next_cursor: Option<String>,
    pub used_bytes: u64,
    pub max_bytes: u64,
}

#[derive(Clone, Default, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Health {
    pub updated_at_ms: u64,
    pub last_frame_at_ms: Option<u64>,
    pub state: String,
    pub reconnects: u64,
    pub frames_observed: u64,
    pub clips_created: u64,
    pub clips_dropped: u64,
    #[serde(default)]
    pub progress: Option<crate::progress::ProgressHealth>,
}

#[derive(Clone)]
pub struct Store {
    settings: Settings,
}
fn private_directory(path: &Path) -> Result<(), Error> {
    #[cfg(unix)]
    {
        if !path.exists() {
            let mut builder = fs::DirBuilder::new();
            builder.mode(0o700);
            builder.create(path)?;
        }
        let meta = fs::symlink_metadata(path)?;
        if !meta.is_dir() || meta.mode() & 0o077 != 0 {
            return Err(Error::Invalid);
        }
        Ok(())
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        Err(Error::Invalid)
    }
}
fn private_options() -> OpenOptions {
    let mut options = OpenOptions::new();
    #[cfg(unix)]
    {
        options
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    options
}
fn read_private(path: &Path, max: u64) -> Result<Vec<u8>, Error> {
    let file = private_options().read(true).open(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            Error::NotFound
        } else {
            Error::Io
        }
    })?;
    let meta = file.metadata()?;
    #[cfg(unix)]
    if meta.mode() & 0o077 != 0 || meta.nlink() != 1 {
        return Err(Error::Corrupt);
    }
    if !meta.is_file() || meta.len() > max {
        return Err(Error::Corrupt);
    }
    let mut bytes = Vec::new();
    file.take(max + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > max {
        return Err(Error::Corrupt);
    }
    Ok(bytes)
}
fn write_new(path: &Path, bytes: &[u8]) -> Result<(), Error> {
    let mut file = private_options().write(true).create_new(true).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}
fn atomic_json<T: Serialize>(path: &Path, value: &T) -> Result<(), Error> {
    let temp = path.with_extension("pending");
    // A previous interrupted atomic write was never published. Caller holds the root lock.
    if temp.exists() {
        let meta = fs::symlink_metadata(&temp)?;
        if !meta.is_file() || meta.file_type().is_symlink() {
            return Err(Error::Corrupt);
        }
        fs::remove_file(&temp)?;
    }
    write_new(
        &temp,
        &serde_json::to_vec(value).map_err(|_| Error::Corrupt)?,
    )?;
    fs::rename(&temp, path)?;
    File::open(path.parent().ok_or(Error::Invalid)?)?.sync_all()?;
    Ok(())
}

impl Store {
    pub fn open(settings: Settings) -> Result<Self, Error> {
        settings.validate()?;
        settings.verify_protection()?;
        private_directory(&settings.directory)?;
        // Reject aliases through symlinks as well as a symlink at the final component.
        if fs::canonicalize(&settings.directory)? != settings.directory {
            return Err(Error::Invalid);
        }
        for name in ["events", "staging", "spool"] {
            private_directory(&settings.directory.join(name))?;
        }
        let store = Self { settings };
        let _guard = store.lock()?;
        store.inventory()?;
        // Only our unpublished/retired directories are recoverable. A corrupt published
        // manifest above stops recovery before any footage is removed.
        for entry in fs::read_dir(store.settings.directory.join("staging"))?.take(MAX_EVENTS + 1) {
            let entry = entry?;
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| Error::Corrupt)?;
            let id = name.strip_prefix("retired-").unwrap_or(&name);
            if !event_id(id) || !entry.file_type()?.is_dir() || entry.file_type()?.is_symlink() {
                return Err(Error::Corrupt);
            }
            fs::remove_dir_all(entry.path())?;
        }
        Ok(store)
    }
    pub fn settings(&self) -> &Settings {
        &self.settings
    }
    fn lock(&self) -> Result<File, Error> {
        self.settings.verify_protection()?;
        let file = private_options()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.settings.directory.join("store.lock"))?;
        #[cfg(unix)]
        {
            let meta = file.metadata()?;
            if !meta.is_file() || meta.nlink() != 1 || meta.mode() & 0o077 != 0 {
                return Err(Error::Corrupt);
            }
        }
        file.try_lock().map_err(|_| Error::Busy)?;
        Ok(file)
    }
    fn inventory(&self) -> Result<Vec<Event>, Error> {
        let mut events = Vec::new();
        for entry in fs::read_dir(self.settings.directory.join("events"))?.take(MAX_EVENTS + 1) {
            let entry = entry?;
            if events.len() == MAX_EVENTS {
                return Err(Error::Corrupt);
            }
            let id = entry
                .file_name()
                .into_string()
                .map_err(|_| Error::Corrupt)?;
            let meta = entry.file_type()?;
            if !event_id(&id) || !meta.is_dir() || meta.is_symlink() {
                return Err(Error::Corrupt);
            }
            let event: Event =
                serde_json::from_slice(&read_private(&entry.path().join("event.json"), 4096)?)
                    .map_err(|_| Error::Corrupt)?;
            event.validate()?;
            if event.event_id != id || !self.settings.camera_ids.contains(&event.camera_id) {
                return Err(Error::Corrupt);
            }
            for (name, expected) in [
                ("clip.mp4", event.clip_bytes),
                ("thumbnail.jpg", event.thumbnail_bytes),
            ] {
                let path = entry.path().join(name);
                let meta = fs::symlink_metadata(path)?;
                if !meta.is_file() || meta.file_type().is_symlink() || meta.len() != expected {
                    return Err(Error::Corrupt);
                }
                #[cfg(unix)]
                if meta.mode() & 0o077 != 0 || meta.nlink() != 1 {
                    return Err(Error::Corrupt);
                }
            }
            events.push(event);
        }
        events.sort_by(|a, b| (b.started_at_ms, &b.event_id).cmp(&(a.started_at_ms, &a.event_id)));
        Ok(events)
    }
    fn path(&self, id: &str) -> PathBuf {
        self.settings.directory.join("events").join(id)
    }
    fn prune_locked(
        &self,
        events: &mut Vec<Event>,
        now: u64,
        reserve: u64,
        adding: bool,
    ) -> Result<(), Error> {
        let mut used: u64 = events.iter().map(Event::bytes).sum();
        let cutoff = now.saturating_sub(self.settings.retention_seconds * 1000);
        // Oldest first. Kept events are excluded from BOTH expiry and quota eviction.
        let candidates: Vec<String> = events
            .iter()
            .rev()
            .filter(|e| !e.kept)
            .map(|e| e.event_id.clone())
            .collect();
        for id in candidates {
            let event = events
                .iter()
                .find(|e| e.event_id == id)
                .ok_or(Error::Corrupt)?;
            if event.ended_at_ms < cutoff
                || used.saturating_add(reserve) > self.settings.max_bytes
                || events.len() + usize::from(adding) > self.settings.max_events
            {
                let size = event.bytes();
                // Atomically remove from the published index before deleting owned files.
                let retired = self
                    .settings
                    .directory
                    .join("staging")
                    .join(format!("retired-{id}"));
                fs::rename(self.path(&id), &retired)?;
                File::open(self.settings.directory.join("events"))?.sync_all()?;
                fs::remove_dir_all(retired)?;
                used -= size;
                events.retain(|e| e.event_id != id);
            }
        }
        if used.saturating_add(reserve) > self.settings.max_bytes
            || events.len() + usize::from(adding) > self.settings.max_events
        {
            return Err(Error::Full);
        }
        Ok(())
    }
    pub fn prune(&self, now: u64) -> Result<(), Error> {
        let _guard = self.lock()?;
        let mut events = self.inventory()?;
        self.prune_locked(&mut events, now, 0, false)
    }
    pub fn publish(
        &self,
        camera: &str,
        start: u64,
        end: u64,
        clip: &[u8],
        thumbnail: &[u8],
        interrupted: bool,
    ) -> Result<Event, Error> {
        if !self.settings.camera_ids.iter().any(|id| id == camera) {
            return Err(Error::Invalid);
        }
        let mut entropy = [0u8; 16];
        getrandom::fill(&mut entropy).map_err(|_| Error::Io)?;
        let event = Event {
            event_id: entropy.iter().map(|b| format!("{b:02x}")).collect(),
            camera_id: camera.into(),
            kind: "motion".into(),
            started_at_ms: start,
            ended_at_ms: end,
            clip_bytes: clip.len() as u64,
            thumbnail_bytes: thumbnail.len() as u64,
            kept: false,
            interrupted,
        };
        event.validate()?;
        let _guard = self.lock()?;
        let mut events = self.inventory()?;
        self.prune_locked(&mut events, crate::now_ms(), event.bytes(), true)?;
        let staging = self
            .settings
            .directory
            .join("staging")
            .join(&event.event_id);
        private_directory(&staging)?;
        let result = (|| {
            write_new(&staging.join("clip.mp4"), clip)?;
            write_new(&staging.join("thumbnail.jpg"), thumbnail)?;
            write_new(
                &staging.join("event.json"),
                &serde_json::to_vec(&event).map_err(|_| Error::Corrupt)?,
            )?;
            File::open(&staging)?.sync_all()?;
            if self.path(&event.event_id).exists() {
                return Err(Error::Corrupt);
            }
            fs::rename(&staging, self.path(&event.event_id))?;
            File::open(self.settings.directory.join("events"))?.sync_all()?;
            Ok(event.clone())
        })();
        if result.is_err() && staging.is_dir() {
            let _ = fs::remove_dir_all(staging);
        }
        result
    }
    pub fn page(&self, camera: &str, before: Option<&str>) -> Result<Page, Error> {
        self.filtered_page(camera, before, false)
    }
    pub fn kept_page(&self, camera: &str, before: Option<&str>) -> Result<Page, Error> {
        self.filtered_page(camera, before, true)
    }
    fn filtered_page(
        &self,
        camera: &str,
        before: Option<&str>,
        kept_only: bool,
    ) -> Result<Page, Error> {
        if !self.settings.camera_ids.iter().any(|v| v == camera) {
            return Err(Error::NotFound);
        }
        let _guard = self.lock()?;
        let events = self.inventory()?;
        let used_bytes = events.iter().map(Event::bytes).sum();
        let camera_events: Vec<_> = events
            .into_iter()
            .filter(|e| e.camera_id == camera && (!kept_only || e.kept))
            .collect();
        let offset = match before {
            Some(id) if event_id(id) => camera_events
                .iter()
                .position(|e| e.event_id == id)
                .map(|i| i + 1)
                .ok_or(Error::NotFound)?,
            Some(_) => return Err(Error::NotFound),
            None => 0,
        };
        let page: Vec<_> = camera_events
            .iter()
            .skip(offset)
            .take(50)
            .cloned()
            .collect();
        let next_cursor = if offset + page.len() < camera_events.len() {
            page.last().map(|e| e.event_id.clone())
        } else {
            None
        };
        Ok(Page {
            events: page,
            next_cursor,
            used_bytes,
            max_bytes: self.settings.max_bytes,
        })
    }
    pub fn media(&self, camera: &str, id: &str, thumbnail: bool) -> Result<Vec<u8>, Error> {
        if !event_id(id) {
            return Err(Error::NotFound);
        }
        let _guard = self.lock()?;
        if !self
            .inventory()?
            .iter()
            .any(|e| e.event_id == id && e.camera_id == camera)
        {
            return Err(Error::NotFound);
        }
        read_private(
            &self.path(id).join(if thumbnail {
                "thumbnail.jpg"
            } else {
                "clip.mp4"
            }),
            if thumbnail {
                MAX_THUMBNAIL_BYTES
            } else {
                MAX_CLIP_BYTES
            },
        )
    }
    /// Bounded authenticated download pieces work through the original 1 MiB control proxy.
    pub fn clip_part(&self, camera: &str, id: &str, part: u32) -> Result<Vec<u8>, Error> {
        const PART: u64 = 512 * 1024;
        if !event_id(id) || part >= 64 {
            return Err(Error::NotFound);
        }
        let _guard = self.lock()?;
        let event = self
            .inventory()?
            .into_iter()
            .find(|e| e.event_id == id && e.camera_id == camera)
            .ok_or(Error::NotFound)?;
        let offset = u64::from(part) * PART;
        if offset >= event.clip_bytes {
            return Err(Error::NotFound);
        }
        let mut file = private_options()
            .read(true)
            .open(self.path(id).join("clip.mp4"))?;
        file.seek(SeekFrom::Start(offset))?;
        let length = (event.clip_bytes - offset).min(PART);
        let mut bytes = Vec::new();
        file.take(length).read_to_end(&mut bytes)?;
        if bytes.len() as u64 != length {
            return Err(Error::Corrupt);
        }
        Ok(bytes)
    }
    pub fn keep(&self, camera: &str, id: &str, kept: bool) -> Result<Event, Error> {
        let _guard = self.lock()?;
        let mut event = self
            .inventory()?
            .into_iter()
            .find(|e| e.event_id == id && e.camera_id == camera)
            .ok_or(Error::NotFound)?;
        event.kept = kept;
        atomic_json(&self.path(id).join("event.json"), &event)?;
        Ok(event)
    }
    pub fn remove(&self, camera: &str, id: &str) -> Result<(), Error> {
        let _guard = self.lock()?;
        if !self
            .inventory()?
            .iter()
            .any(|e| e.event_id == id && e.camera_id == camera)
        {
            return Err(Error::NotFound);
        }
        let retired = self
            .settings
            .directory
            .join("staging")
            .join(format!("retired-{id}"));
        fs::rename(self.path(id), &retired)?;
        File::open(self.settings.directory.join("events"))?.sync_all()?;
        fs::remove_dir_all(retired)?;
        Ok(())
    }
    pub fn write_health(&self, camera: &str, health: &Health) -> Result<(), Error> {
        if !self.settings.camera_ids.iter().any(|v| v == camera) {
            return Err(Error::Invalid);
        }
        let _guard = self.lock()?;
        atomic_json(
            &self
                .settings
                .directory
                .join(format!("health-{camera}.json")),
            health,
        )
    }
    pub fn health(&self, camera: &str) -> Result<Option<Health>, Error> {
        if !self.settings.camera_ids.iter().any(|v| v == camera) {
            return Err(Error::NotFound);
        }
        let _guard = self.lock()?;
        match read_private(
            &self
                .settings
                .directory
                .join(format!("health-{camera}.json")),
            4096,
        ) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map(Some)
                .map_err(|_| Error::Corrupt),
            Err(Error::NotFound) => Ok(None),
            Err(e) => Err(e),
        }
    }
}
