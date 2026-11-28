//! Caller-driven UTC discipline. Sources supply UTC at a local `Instant` with
//! an assumed absolute error bound. This owner alone controls the wall clock;
//! reads, waits, source acquisition and product scheduling stay with Rust std
//! and the caller. See the crate README for lifecycle and trust requirements.
#![forbid(unsafe_code)]

use std::time::{Duration, Instant, SystemTime};
use time::Control;

const QUANTUM: Duration = Duration::from_micros(1);

#[derive(Clone, Copy, Debug)]
pub struct Policy {
    /// Maximum measured absolute UTC error, including all uncertainty bounds.
    pub accuracy: Duration,
    /// Request another observation at least this often while awake.
    pub refresh_interval: Duration,
    /// Stop extrapolating a source after this age, even if no replacement exists.
    pub max_holdover: Duration,
    /// Largest permitted correction, expressed as time at the provider's rate.
    pub max_slew_time: Duration,
    /// Bound on paired SystemTime/Instant read quantization for this clock.
    pub clock_resolution: Duration,
    /// Assumed oscillator frequency error relative to actual elapsed time, not
    /// the correction rate. Extrapolation includes the slow-clock worst case.
    pub drift_ppm: u32,
}

#[derive(Clone, Copy, Debug)]
pub struct Source {
    /// Lower values are preferred within the same freshness tier. The caller
    /// admits/trusts sources; self-reported uncertainty does not establish trust.
    pub priority: u8,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InitialTime {
    /// Permit one intentional seed step on the first admitted observation.
    Unknown,
    /// Preserve an existing clock; its quality still needs to be verified.
    Preserve,
}

/// Capture this before starting a source measurement. Never relabel a delayed
/// pre-sleep exchange with the current epoch. Tokens belong to one controller.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Epoch(u64);

#[derive(Clone, Copy, Debug)]
pub struct Observation {
    pub source: usize,
    pub epoch: Epoch,
    /// Local monotonic instant corresponding to `utc`, not delivery time.
    pub at: Instant,
    /// POSIX UTC; source adapters must use a consistent leap-second convention.
    pub utc: SystemTime,
    /// Assumed absolute error bound including capture and transport error.
    pub uncertainty: Duration,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Unknown,
    /// A clock change was accepted; another paired read must verify it.
    Checking,
    Adjusting,
    Synchronized,
    /// Reference needs renewal or no longer establishes the requested accuracy.
    Holdover,
    StepRequired,
    Conflict,
    Fault,
    Suspended,
}

/// Historical measurement, valid at `checked_at`, not a live promise. Call
/// `poll` to obtain current quality. A due `refresh_at` requests source work;
/// the caller owns acquisition backoff and must not busy-poll that deadline.
#[derive(Clone, Copy, Debug)]
pub struct Status {
    pub initialized: bool,
    pub state: State,
    pub checked_at: Instant,
    pub source: Option<usize>,
    pub reference_age: Option<Duration>,
    pub error_bound: Option<Duration>,
    pub offset_us: Option<i64>,
    pub pending_us: Option<i64>,
    pub refresh_at: Instant,
    /// Check completion at this instant; `None` means await a source/lifecycle
    /// event. An immediate deadline after a write requests one readback.
    pub check_at: Option<Instant>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidPolicy,
    InvalidSource,
    WrongEpoch,
    Suspended,
    NotSuspended,
    Future,
    Stale,
    Reordered,
    Uncertain,
    OutOfRange,
    Conflict,
    StepRequired { offset_us: i64 },
    Backend(time::Error),
}

/// One synchronous mutation owner with exactly N source slots, no allocations,
/// worker or queue. Sources with fresh observations precede holdover sources;
/// priority then uncertainty select within that tier. Disjoint UTC intervals
/// from equally preferred sources stop correction instead of silently voting.
pub struct Controller<C, const N: usize> {
    control: C,
    policy: Policy,
    sources: [Source; N],
    observations: [Option<Observation>; N],
    rate: Option<u32>,
    epoch: Epoch,
    minimum_at: Instant,
    seed_allowed: bool,
    suspended: bool,
    status: Status,
}

impl<C: Control, const N: usize> Controller<C, N> {
    /// Cancel an inherited adjustment before accepting sources. `Preserve`
    /// prevents startup steps but does not certify RTC validity or UTC quality.
    pub fn new(
        mut control: C,
        policy: Policy,
        sources: [Source; N],
        initial: InitialTime,
    ) -> Result<Self, Error> {
        let rate = control.capabilities().slew_rate_ppm;
        if N == 0
            || policy.refresh_interval.is_zero()
            || policy.max_holdover < policy.refresh_interval
            || policy.max_slew_time.is_zero()
            || policy.clock_resolution.is_zero()
            || policy
                .clock_resolution
                .checked_add(QUANTUM)
                .ok_or(Error::InvalidPolicy)?
                >= policy.accuracy
            || policy.drift_ppm >= 1_000_000
            || rate == Some(0)
            || rate.is_some_and(|rate| rate >= 1_000_000)
        {
            return Err(Error::InvalidPolicy);
        }
        let now = Instant::now();
        now.checked_add(policy.max_holdover)
            .ok_or(Error::InvalidPolicy)?;
        now.checked_add(policy.max_slew_time)
            .ok_or(Error::InvalidPolicy)?;
        if rate.is_some() {
            control.cancel_adjustment().map_err(Error::Backend)?;
        }
        let now = Instant::now();
        let initialized = initial == InitialTime::Preserve;
        Ok(Self {
            control,
            policy,
            sources,
            observations: [None; N],
            rate,
            epoch: Epoch(0),
            minimum_at: now,
            seed_allowed: !initialized,
            suspended: false,
            status: empty_status(now, initialized, State::Unknown),
        })
    }

    pub fn epoch(&self) -> Epoch {
        self.epoch
    }

    pub fn last_status(&self) -> Status {
        self.status
    }

    pub fn observe(&mut self, observation: Observation) -> Result<Status, Error> {
        self.admit(observation, Instant::now())?;
        self.poll()
    }

    /// Query progress, read the actual clock and reconcile with retained source
    /// evidence. A zero pending correction alone never establishes convergence.
    pub fn poll(&mut self) -> Result<Status, Error> {
        if self.suspended {
            return Err(Error::Suspended);
        }
        // Read wall time after the query has committed native progress.
        let pending = self
            .pending()
            .map_err(|error| self.fault(error, Instant::now()))?;
        let reading = Reading::now(self.policy.clock_resolution)
            .map_err(|error| self.fault(error, Instant::now()))?;
        self.reconcile(reading, pending)
    }

    /// Invalidate observations even when cancellation fails. The caller must
    /// handle that failure before sleeping. Drop does not perform this work.
    pub fn suspend(&mut self) -> Result<(), Error> {
        self.suspended = true;
        self.observations.fill(None);
        self.status = empty_status(Instant::now(), self.status.initialized, State::Suspended);
        if self.rate.is_some() {
            self.control.cancel_adjustment().map_err(Error::Backend)?;
            self.status.pending_us = Some(0);
        }
        Ok(())
    }

    /// Start a new source epoch after the board/OS has restored clock accounting.
    /// This does not restore an RTC, establish elapsed sleep time or authorize a
    /// step. After reset, construct a new controller instead of persisting Instant.
    pub fn resume(&mut self) -> Result<Epoch, Error> {
        if !self.suspended {
            return Err(Error::NotSuspended);
        }
        let epoch = Epoch(self.epoch.0.checked_add(1).ok_or(Error::OutOfRange)?);
        if self.rate.is_some() {
            self.control.cancel_adjustment().map_err(Error::Backend)?;
        }
        self.minimum_at = Instant::now();
        self.epoch = epoch;
        self.suspended = false;
        let state = if self.status.initialized {
            State::Holdover
        } else {
            State::Unknown
        };
        self.status = empty_status(self.minimum_at, self.status.initialized, state);
        Ok(epoch)
    }

    fn admit(&mut self, observation: Observation, now: Instant) -> Result<(), Error> {
        if self.suspended {
            return Err(Error::Suspended);
        }
        if observation.epoch != self.epoch || observation.at < self.minimum_at {
            return Err(Error::WrongEpoch);
        }
        let slot = self
            .observations
            .get(observation.source)
            .ok_or(Error::InvalidSource)?;
        let age = now
            .checked_duration_since(observation.at)
            .ok_or(Error::Future)?;
        if age > self.policy.max_holdover {
            return Err(Error::Stale);
        }
        if slot.is_some_and(|previous| observation.at <= previous.at) {
            return Err(Error::Reordered);
        }
        let bound = observation
            .uncertainty
            .checked_add(self.policy.clock_resolution)
            .and_then(|value| value.checked_add(QUANTUM))
            .ok_or(Error::OutOfRange)?;
        if bound >= self.policy.accuracy {
            return Err(Error::Uncertain);
        }
        self.project(observation, now)?;
        self.observations[observation.source] = Some(observation);
        Ok(())
    }

    fn pending(&mut self) -> Result<i64, Error> {
        match self.rate {
            Some(_) => self.control.pending_adjustment().map_err(Error::Backend),
            None => Ok(0),
        }
    }

    fn cancel(&mut self, pending: i64) -> Result<(), Error> {
        if pending != 0 {
            self.control.cancel_adjustment().map_err(Error::Backend)?;
        }
        Ok(())
    }

    fn project(&self, observation: Observation, now: Instant) -> Result<Reference, Error> {
        let age = now
            .checked_duration_since(observation.at)
            .ok_or(Error::Future)?;
        let uncertainty = observation
            .uncertainty
            .checked_add(scale(
                age,
                self.policy.drift_ppm,
                1_000_000 - self.policy.drift_ppm,
            )?)
            .ok_or(Error::OutOfRange)?;
        Ok(Reference {
            observation,
            utc: observation.utc.checked_add(age).ok_or(Error::OutOfRange)?,
            uncertainty,
            age,
            fresh: age < self.policy.refresh_interval,
        })
    }

    fn select(&self, now: Instant) -> Result<Option<Reference>, Error> {
        let mut best: Option<Reference> = None;
        for observation in self.observations.iter().flatten() {
            if now
                .checked_duration_since(observation.at)
                .ok_or(Error::Future)?
                > self.policy.max_holdover
            {
                continue;
            }
            let reference = self.project(*observation, now)?;
            if best.is_none_or(|old| self.rank(reference) < self.rank(old)) {
                best = Some(reference);
            }
        }
        if let Some(best) = best {
            let mut lower = best
                .utc
                .checked_sub(best.uncertainty)
                .ok_or(Error::OutOfRange)?;
            let mut upper = best
                .utc
                .checked_add(best.uncertainty)
                .ok_or(Error::OutOfRange)?;
            for observation in self.observations.iter().flatten() {
                if now
                    .checked_duration_since(observation.at)
                    .ok_or(Error::Future)?
                    > self.policy.max_holdover
                {
                    continue;
                }
                let other = self.project(*observation, now)?;
                if other.fresh == best.fresh
                    && self.sources[other.observation.source].priority
                        == self.sources[best.observation.source].priority
                {
                    lower = lower.max(
                        other
                            .utc
                            .checked_sub(other.uncertainty)
                            .ok_or(Error::OutOfRange)?,
                    );
                    upper = upper.min(
                        other
                            .utc
                            .checked_add(other.uncertainty)
                            .ok_or(Error::OutOfRange)?,
                    );
                    if lower > upper {
                        return Err(Error::Conflict);
                    }
                }
            }
        }
        Ok(best)
    }

    fn rank(&self, reference: Reference) -> (bool, u8, Duration) {
        (
            !reference.fresh,
            self.sources[reference.observation.source].priority,
            reference.uncertainty,
        )
    }

    fn reconcile(&mut self, reading: Reading, pending: i64) -> Result<Status, Error> {
        self.discipline(reading, pending)
            .map_err(|error| match error {
                Error::Conflict | Error::StepRequired { .. } => error,
                _ => self.fault(error, reading.at),
            })
    }

    fn discipline(&mut self, reading: Reading, pending: i64) -> Result<Status, Error> {
        if reading.uncertainty >= self.policy.accuracy {
            return Err(Error::Uncertain);
        }
        let reference = match self.select(reading.at) {
            Ok(Some(reference)) => reference,
            Ok(None) => {
                self.cancel(pending)?;
                let state = if self.status.initialized {
                    State::Holdover
                } else {
                    State::Unknown
                };
                self.status = empty_status(reading.at, self.status.initialized, state);
                self.status.pending_us = self.rate.map(|_| 0);
                return Ok(self.status);
            }
            Err(Error::Conflict) => {
                self.cancel(pending)?;
                self.status = empty_status(reading.at, self.status.initialized, State::Conflict);
                self.status.pending_us = self.rate.map(|_| 0);
                return Err(Error::Conflict);
            }
            Err(error) => return Err(error),
        };
        let (negative, distance) = difference(reference.utc, reading.utc);
        let offset_us = i64::try_from(distance.as_micros()).map_err(|_| Error::OutOfRange)?;
        let offset_us = if negative { -offset_us } else { offset_us };
        let uncertainty = reference
            .uncertainty
            .checked_add(reading.uncertainty)
            .and_then(|value| value.checked_add(QUANTUM))
            .ok_or(Error::OutOfRange)?;
        let error_bound = distance.checked_add(uncertainty).ok_or(Error::OutOfRange)?;
        self.status = Status {
            initialized: self.status.initialized,
            state: State::Holdover,
            checked_at: reading.at,
            source: Some(reference.observation.source),
            reference_age: Some(reference.age),
            error_bound: Some(error_bound),
            offset_us: Some(offset_us),
            pending_us: self.rate.map(|_| pending),
            refresh_at: self.refresh_at(reference.observation)?,
            check_at: None,
        };
        if self.seed_allowed {
            if uncertainty >= self.policy.accuracy {
                // A once-accurate sample may have exhausted its bound in
                // transit/holdover. Keep the one seed for fresh evidence.
                self.cancel(pending)?;
                self.status.state = State::Unknown;
                self.status.pending_us = self.rate.map(|_| 0);
                self.status.refresh_at = reading.at;
                return Ok(self.status);
            }
            // An ambiguous set failure must not silently grant another step.
            self.seed_allowed = false;
            self.control.set(reference.utc).map_err(Error::Backend)?;
            self.status.initialized = true;
            self.changed(reading.at, State::Checking, 0);
        } else if error_bound <= self.policy.accuracy || distance <= uncertainty {
            self.cancel(pending)?;
            if pending != 0 {
                self.changed(reading.at, State::Checking, 0);
            } else if error_bound <= self.policy.accuracy {
                self.status.initialized = true;
                self.status.state = if reference.fresh {
                    State::Synchronized
                } else {
                    State::Holdover
                };
                if reference.fresh {
                    self.status.check_at = Some(self.status.refresh_at);
                }
            }
        } else if self.rate.is_none()
            || distance > scale(self.policy.max_slew_time, self.rate.unwrap_or(0), 1_000_000)?
        {
            self.cancel(pending)?;
            self.status.pending_us = self.rate.map(|_| 0);
            self.status.state = State::StepRequired;
            return Err(Error::StepRequired { offset_us });
        } else {
            let disagreement = i128::from(offset_us) - i128::from(pending);
            // The measured offset already includes applied slew. Never add or
            // subtract the old pending correction to form a replacement.
            if pending == 0 || disagreement.unsigned_abs() > uncertainty.as_micros() {
                self.control
                    .replace_adjustment(offset_us)
                    .map_err(Error::Backend)?;
                self.changed(reading.at, State::Adjusting, offset_us);
            } else {
                self.status.state = State::Adjusting;
            }
            let remaining =
                Duration::from_micros(self.status.pending_us.unwrap_or(0).unsigned_abs());
            let wait = scale(remaining, 1_000_000, self.rate.unwrap_or(1))?
                .min(self.policy.refresh_interval);
            self.status.check_at = Some(reading.at.checked_add(wait).ok_or(Error::OutOfRange)?);
        }
        Ok(self.status)
    }

    fn refresh_at(&self, observation: Observation) -> Result<Instant, Error> {
        let base = observation
            .uncertainty
            .checked_add(self.policy.clock_resolution)
            .and_then(|value| value.checked_add(QUANTUM))
            .ok_or(Error::OutOfRange)?;
        let margin = self.policy.accuracy.saturating_sub(base);
        let interval = if self.policy.drift_ppm == 0 {
            self.policy.refresh_interval
        } else {
            // Round down: ask for a sample before drift consumes the budget.
            nanos(
                margin
                    .as_nanos()
                    .checked_mul(u128::from(1_000_000 - self.policy.drift_ppm))
                    .ok_or(Error::OutOfRange)?
                    / u128::from(self.policy.drift_ppm),
            )?
            .min(self.policy.refresh_interval)
        };
        observation
            .at
            .checked_add(interval)
            .ok_or(Error::OutOfRange)
    }

    fn changed(&mut self, at: Instant, state: State, pending: i64) {
        self.status.state = state;
        self.status.pending_us = self.rate.map(|_| pending);
        self.status.error_bound = None;
        self.status.check_at = Some(at);
    }

    fn fault(&mut self, error: Error, at: Instant) -> Error {
        self.status = empty_status(at, self.status.initialized, State::Fault);
        error
    }
}

#[derive(Clone, Copy)]
struct Reading {
    at: Instant,
    utc: SystemTime,
    uncertainty: Duration,
}

impl Reading {
    fn now(resolution: Duration) -> Result<Self, Error> {
        let begin = Instant::now();
        let utc = SystemTime::now();
        let at = Instant::now();
        // UTC was read somewhere in the bracket. Using its end and the entire
        // bracket is conservative without assuming symmetric read latency.
        let uncertainty = at
            .checked_duration_since(begin)
            .ok_or(Error::OutOfRange)?
            .checked_add(resolution)
            .ok_or(Error::OutOfRange)?;
        Ok(Self {
            at,
            utc,
            uncertainty,
        })
    }
}

#[derive(Clone, Copy)]
struct Reference {
    observation: Observation,
    utc: SystemTime,
    age: Duration,
    uncertainty: Duration,
    fresh: bool,
}

fn empty_status(at: Instant, initialized: bool, state: State) -> Status {
    Status {
        initialized,
        state,
        checked_at: at,
        source: None,
        reference_age: None,
        error_bound: None,
        offset_us: None,
        pending_us: None,
        refresh_at: at,
        check_at: None,
    }
}

fn difference(left: SystemTime, right: SystemTime) -> (bool, Duration) {
    match left.duration_since(right) {
        Ok(distance) => (false, distance),
        Err(error) => (true, error.duration()),
    }
}

fn nanos(value: u128) -> Result<Duration, Error> {
    let seconds = u64::try_from(value / 1_000_000_000).map_err(|_| Error::OutOfRange)?;
    Ok(Duration::new(seconds, (value % 1_000_000_000) as u32))
}

fn scale(value: Duration, numerator: u32, denominator: u32) -> Result<Duration, Error> {
    let product = value
        .as_nanos()
        .checked_mul(u128::from(numerator))
        .ok_or(Error::OutOfRange)?;
    nanos(product.div_ceil(u128::from(denominator)))
}

#[cfg(test)]
mod tests;
