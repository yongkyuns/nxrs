use camera_service::Frames;
use telemetry_service::Telemetry;
use crate::{reader::Reader, AppState, AppStats, Error, Progress};

/// Composes Frames + Telemetry; needs neither storage nor recorder state.
#[derive(Default)]
pub struct Monitor { reader: Reader }

impl Monitor {
    pub fn start(&mut self, frames: &impl Frames) -> Result<(), Error> { self.reader.start(frames) }
    pub fn state(&self) -> AppState { self.reader.state }
    pub fn stats(&self) -> AppStats { self.reader.stats }

    pub fn step(&mut self, frames: &impl Frames, telemetry: &mut impl Telemetry) -> Result<Progress, Error> {
        if self.reader.state != AppState::Running { return Ok(Progress::Idle); }
        let Some(frame) = frames.next_after(self.reader.sequence) else { return Ok(Progress::Idle); };
        telemetry.publish(frame)?;
        Ok(self.reader.accept(frame.sequence))
    }

    pub fn stop(&mut self) { self.reader.state = AppState::Stopped; }
}
