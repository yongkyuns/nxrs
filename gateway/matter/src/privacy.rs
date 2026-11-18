//! Per-camera Matter privacy settings shared by control and media tasks.
use tokio::sync::watch;

mod microphone;
pub use microphone::Microphone;

#[derive(Clone, Copy)]
pub enum Setting {
    MicrophoneMuted = 1,
    LivePrivacy = 2,
    RecordingPrivacy = 4,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    InvalidRecord,
    Storage,
}

#[derive(Clone, Copy)]
struct State {
    flags: u8,
    live_epoch: u64,
    audio_epoch: u64,
    failed: bool,
}

/// One owner per camera. Reads, durable writes and media admission see one
/// setting value. Each viewer waits on a bounded watch notification.
#[derive(Clone)]
pub struct Privacy(watch::Sender<State>);

impl Privacy {
    pub fn load(record: Option<&[u8]>) -> Result<Self, Error> {
        let flags = match record {
            None => Setting::MicrophoneMuted as u8,
            Some(&[1, flags]) if flags & !7 == 0 => flags,
            _ => return Err(Error::InvalidRecord),
        };
        Ok(Self(watch::Sender::new(State {
            flags,
            live_epoch: 0,
            audio_epoch: 0,
            failed: false,
        })))
    }

    pub fn get(&self, setting: Setting) -> bool {
        let state = self.0.borrow();
        state.failed || state.flags & setting as u8 != 0
    }

    /// Publish a change only after persistence succeeds. Ambiguous storage
    /// failure immediately closes admission and retires existing viewers.
    pub fn set(
        &self,
        setting: Setting,
        enabled: bool,
        save: impl FnOnce(&[u8]) -> Result<(), Error>,
    ) -> Result<(), Error> {
        let bit = setting as u8;
        let mut result = Ok(());
        self.0.send_if_modified(|state| {
            if state.failed {
                result = Err(Error::Storage);
                return false;
            }
            let flags = if enabled {
                state.flags | bit
            } else {
                state.flags & !bit
            };
            if flags == state.flags {
                return false;
            }
            if save(&[1, flags]).is_err() {
                state.failed = true;
                result = Err(Error::Storage);
            } else {
                state.flags = flags;
                match setting {
                    Setting::LivePrivacy if enabled => state.live_epoch += 1,
                    Setting::MicrophoneMuted => state.audio_epoch += 1,
                    _ => {}
                }
            }
            true
        });
        result
    }

    /// Wake the owner of pending offers when privacy changes.
    pub async fn observe(&self, mut changed: impl AsyncFnMut()) {
        let mut updates = self.0.subscribe();
        changed().await;
        while updates.changed().await.is_ok() {
            changed().await;
        }
    }

    pub fn live_view(&self) -> Option<LiveView> {
        let state = self.0.subscribe();
        let initial = *state.borrow();
        (!initial.failed && initial.flags & Setting::LivePrivacy as u8 == 0).then_some(LiveView {
            state,
            live_epoch: initial.live_epoch,
            audio_epoch: initial.audio_epoch,
        })
    }
}

pub struct LiveView {
    state: watch::Receiver<State>,
    live_epoch: u64,
    audio_epoch: u64,
}

impl LiveView {
    pub fn retired(&self) -> bool {
        let state = self.state.borrow();
        state.failed || state.live_epoch != self.live_epoch
    }

    pub fn microphone_muted(&self) -> bool {
        self.state.borrow().flags & Setting::MicrophoneMuted as u8 != 0
    }

    /// Discard queued audio even when mute and unmute both occurred while the peer slept.
    pub fn audio_changed(&mut self) -> bool {
        let epoch = self.state.borrow().audio_epoch;
        std::mem::replace(&mut self.audio_epoch, epoch) != epoch
    }

    pub async fn changed(&mut self) {
        let _ = self.state.changed().await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retired_sessions_cannot_reappear_after_unblocking_or_storage_failure() {
        let policy = Privacy::load(None).unwrap();
        let mut old = policy.live_view().unwrap();
        assert!(old.microphone_muted());
        let mut record = Vec::new();
        policy
            .set(Setting::MicrophoneMuted, false, |b| {
                record = b.to_vec();
                Ok(())
            })
            .unwrap();
        assert!(old.audio_changed());
        assert!(!Privacy::load(Some(&record))
            .unwrap()
            .get(Setting::MicrophoneMuted));
        policy.set(Setting::LivePrivacy, true, |_| Ok(())).unwrap();
        assert!(policy.live_view().is_none());
        policy.set(Setting::LivePrivacy, false, |_| Ok(())).unwrap();
        let current = policy.live_view().unwrap();
        assert!(old.retired());
        assert!(!current.retired());
        assert_eq!(
            policy.set(Setting::MicrophoneMuted, true, |_| Err(Error::Storage)),
            Err(Error::Storage)
        );
        assert!(current.retired() && policy.live_view().is_none());
        assert!(Privacy::load(Some(&[1, 8])).is_err());
    }
}
