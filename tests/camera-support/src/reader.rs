use camera_service::Frames;
use crate::{AppState, AppStats, Error, Progress};

#[derive(Default)]
pub(crate) struct Reader { pub(crate) state: AppState, pub(crate) sequence: u64, pub(crate) stats: AppStats }

impl Reader {
    pub(crate) fn start(&mut self, source: &impl Frames) -> Result<(), Error> {
        if self.state != AppState::Stopped { return Err(Error::AlreadyRunning); }
        // Starting/restarting joins the live stream, not retained old history.
        self.sequence = source.latest_sequence();
        self.state = AppState::Running;
        Ok(())
    }

    pub(crate) fn accept(&mut self, sequence: u64) -> Progress {
        let skipped = sequence.saturating_sub(self.sequence).saturating_sub(1);
        self.sequence = sequence;
        self.stats.processed = self.stats.processed.saturating_add(1);
        self.stats.skipped = self.stats.skipped.saturating_add(skipped);
        Progress::Processed { sequence, skipped }
    }
}
