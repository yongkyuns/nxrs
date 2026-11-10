use camera_service::Frames;
use recording_service::Recordings;
use crate::{reader::Reader, AppState, AppStats, Error, Progress};

/// Composes Frames + Recordings. It cannot shut down its frame provider.
/// Dependencies are explicit step arguments, so another product can wire these
/// same handlers without using CameraProduct at all.
#[derive(Default)]
pub struct Recorder { reader: Reader }

impl Recorder {
    pub fn start(&mut self, frames: &impl Frames) -> Result<(), Error> { self.reader.start(frames) }
    pub fn state(&self) -> AppState { self.reader.state }
    pub fn stats(&self) -> AppStats { self.reader.stats }

    /// One bounded unit of work. Failed acceptance leaves the cursor unchanged.
    /// A later retry may see a gap if history was overwritten meanwhile.
    pub fn step(&mut self, frames: &impl Frames, recordings: &mut impl Recordings) -> Result<Progress, Error> {
        if self.reader.state != AppState::Running { return Ok(Progress::Idle); }
        let Some(frame) = frames.next_after(self.reader.sequence) else { return Ok(Progress::Idle); };
        recordings.record(frame)?;
        Ok(self.reader.accept(frame.sequence))
    }

    /// Stops accepting new frames and flushes accepted records, not unread
    /// history. A failed flush remains Stopping and is retryable.
    pub fn stop(&mut self, recordings: &mut impl Recordings) -> Result<(), Error> {
        if self.reader.state == AppState::Stopped { return Ok(()); }
        self.reader.state = AppState::Stopping;
        recordings.flush()?;
        self.reader.state = AppState::Stopped;
        Ok(())
    }
}
