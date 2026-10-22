//! Person-presence inference and security-event policy. Camera ownership stays
//! with its existing acquisition service; inference only receives owned copies.
#![forbid(unsafe_code)]

mod worker;
pub use worker::{Offer, Worker, WorkerOptions, WorkerStats};
#[cfg(feature = "person-model")]
pub mod person;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Configuration,
    Frame,
    Model,
    Clock,
    Worker,
    Stopped,
}

pub use detector::{Bounds, Detection, Detections, Score};

/// Compatibility name for the capability-owned common detector contract.
pub use detector::Detector as PersonDetector;

impl From<detector::DeviceError> for Error {
    fn from(error: detector::DeviceError) -> Self {
        match error {
            detector::DeviceError::Configuration => Self::Configuration,
            detector::DeviceError::Frame => Self::Frame,
            detector::DeviceError::Model | detector::DeviceError::Unsupported => Self::Model,
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Observation {
    pub sequence: u64,
    pub captured_ms: u64,
    pub inference_us: u64,
    pub score: Score,
    pub objects: Detections,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Presence {
    #[default]
    Unavailable,
    Clear,
    Person,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Event {
    PersonEntered,
    PersonLeft,
    Unavailable,
}

#[derive(Clone, Copy, Debug)]
pub struct Policy {
    pub enter_permille: u16,
    pub leave_permille: u16,
    pub confirmation_samples: u8,
    pub max_age_ms: u64,
    pub max_confirmation_gap_ms: u64,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            enter_permille: 500,
            leave_permille: 300,
            confirmation_samples: 2,
            max_age_ms: 5000,
            max_confirmation_gap_ms: 4000,
        }
    }
}

/// Hysteresis and consecutive fresh evidence prevent a single noisy inference
/// from toggling alarms. Expired/failed inference is unknown, never "clear".
pub struct Monitor {
    policy: Policy,
    localization: bool,
    presence: Presence,
    latest: Option<Observation>,
    candidate: Option<(Presence, u8)>,
    now_ms: u64,
    last_sequence: u64,
    last_capture_ms: u64,
}
impl Monitor {
    pub fn new(policy: Policy) -> Result<Self, Error> {
        if policy.enter_permille > 1000
            || policy.leave_permille >= policy.enter_permille
            || policy.confirmation_samples == 0
            || policy.max_age_ms == 0
            || policy.max_confirmation_gap_ms == 0
        {
            return Err(Error::Configuration);
        }
        Ok(Self {
            policy,
            localization: false,
            presence: Presence::Unavailable,
            latest: None,
            candidate: None,
            now_ms: 0,
            last_sequence: 0,
            last_capture_ms: 0,
        })
    }
    /// Localized models report accepted boxes separately from their raw peak
    /// score. Empty accepted output is fresh negative evidence even when a
    /// rejected candidate's score sits inside the presence-only hysteresis band.
    pub fn with_capabilities(
        policy: Policy,
        capabilities: detector::Capabilities,
    ) -> Result<Self, Error> {
        let mut monitor = Self::new(policy)?;
        monitor.localization = capabilities.localization;
        Ok(monitor)
    }
    pub fn presence(&self) -> Presence {
        self.presence
    }
    pub fn latest(&self) -> Option<Observation> {
        self.latest
    }
    fn clock(&mut self, now_ms: u64) -> Result<(), Error> {
        if now_ms < self.now_ms {
            return Err(Error::Clock);
        }
        self.now_ms = now_ms;
        Ok(())
    }
    pub fn tick(&mut self, now_ms: u64) -> Result<Option<Event>, Error> {
        self.clock(now_ms)?;
        if self
            .latest
            .is_some_and(|v| now_ms.saturating_sub(v.captured_ms) > self.policy.max_age_ms)
        {
            return Ok(self.unavailable());
        }
        Ok(None)
    }
    pub fn unavailable(&mut self) -> Option<Event> {
        self.candidate = None;
        self.latest = None;
        if self.presence == Presence::Unavailable {
            return None;
        }
        self.presence = Presence::Unavailable;
        Some(Event::Unavailable)
    }
    pub fn observe(
        &mut self,
        observation: Observation,
        now_ms: u64,
    ) -> Result<Option<Event>, Error> {
        self.clock(now_ms)?;
        if observation.sequence <= self.last_sequence
            || observation.captured_ms > now_ms
            || observation.captured_ms < self.last_capture_ms
        {
            return Err(Error::Frame);
        }
        self.last_sequence = observation.sequence;
        self.last_capture_ms = observation.captured_ms;
        if now_ms - observation.captured_ms > self.policy.max_age_ms {
            return Ok(self.unavailable());
        }
        if self.latest.is_some_and(|v| {
            observation.captured_ms - v.captured_ms > self.policy.max_confirmation_gap_ms
        }) {
            self.candidate = None;
        }
        self.latest = Some(observation);
        let desired = if self.localization {
            if observation.objects.is_empty() {
                Presence::Clear
            } else {
                Presence::Person
            }
        } else if observation.score.permille() >= self.policy.enter_permille {
            Presence::Person
        } else if observation.score.permille() <= self.policy.leave_permille {
            Presence::Clear
        } else {
            self.candidate = None;
            return Ok(None);
        };
        if desired == self.presence {
            self.candidate = None;
            return Ok(None);
        }
        let count = match self.candidate {
            Some((state, count)) if state == desired => count.saturating_add(1),
            _ => 1,
        };
        self.candidate = Some((desired, count));
        if count < self.policy.confirmation_samples {
            return Ok(None);
        }
        let previous = self.presence;
        self.presence = desired;
        self.candidate = None;
        Ok(match desired {
            Presence::Person => Some(Event::PersonEntered),
            Presence::Clear if previous == Presence::Person => Some(Event::PersonLeft),
            _ => None,
        })
    }
}
