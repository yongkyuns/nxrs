//! Own configured acquisition sessions and track normalized observations.
//! HALs acquire and deliver samples. The caller dispatches events and deadlines.

use messaging::{DeliveryError, DeliveryResult, Rejected, Sender};
use std::sync::mpsc::TrySendError;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Reading<T> {
    Waiting,
    Live(T),
    Stale,
    Fault,
}

#[derive(Clone, Copy)]
enum Latest<T> {
    Waiting,
    Sample { value: T, received: Instant },
    Fault,
}

#[derive(Clone, Copy)]
struct Source<T> {
    latest: Latest<T>,
    fresh_for: Duration,
}

impl<T: Copy> Source<T> {
    fn new(fresh_for: Duration) -> Self {
        Self {
            latest: Latest::Waiting,
            fresh_for,
        }
    }

    fn receive(&mut self, value: T, received: Instant) {
        if !matches!(self.latest, Latest::Fault) {
            self.latest = Latest::Sample { value, received };
        }
    }

    fn fail(&mut self) -> bool {
        let first = !matches!(self.latest, Latest::Fault);
        self.latest = Latest::Fault;
        first
    }

    fn reading(&self, now: Instant) -> Reading<T> {
        match self.latest {
            Latest::Waiting => Reading::Waiting,
            Latest::Sample { value, received }
                if now.saturating_duration_since(received) < self.fresh_for =>
            {
                Reading::Live(value)
            }
            Latest::Sample { .. } => Reading::Stale,
            Latest::Fault => Reading::Fault,
        }
    }
}

/// Observations for one acquisition lifetime. Faults remain visible for that
/// lifetime, even if a sample that was already queued arrives afterward.
pub struct Observations<const IMUS: usize> {
    imu: [Source<imu::ImuSample>; IMUS],
    gnss: Option<Source<gnss::GnssFix>>,
}

#[derive(Clone, Copy)]
pub struct Snapshot<const IMUS: usize> {
    pub imu: [Reading<imu::ImuSample>; IMUS],
    /// An unconfigured receiver has no reading. A configured receiver without a
    /// fix reports `Waiting`.
    pub gnss: Option<Reading<gnss::GnssFix>>,
}

impl<const IMUS: usize> Observations<IMUS> {
    pub fn new(imu_fresh_for: Duration, gnss_fresh_for: Option<Duration>) -> Self {
        Self {
            imu: [Source::new(imu_fresh_for); IMUS],
            gnss: gnss_fresh_for.map(Source::new),
        }
    }

    /// Record a sample for an IMU ordinal configured by the caller.
    ///
    /// # Panics
    ///
    /// Panics if `index` is outside the configured IMU array.
    pub fn imu_sample(&mut self, index: usize, sample: imu::ImuSample, received: Instant) {
        self.imu[index].receive(sample, received);
    }

    /// Return whether this is the first fault for the configured IMU ordinal.
    ///
    /// # Panics
    ///
    /// Panics if `index` is outside the configured IMU array.
    pub fn imu_failed(&mut self, index: usize) -> bool {
        self.imu[index].fail()
    }

    pub fn gnss_fix(&mut self, fix: gnss::GnssFix, received: Instant) {
        if let Some(source) = &mut self.gnss {
            source.receive(fix, received);
        }
    }

    /// Return whether the configured receiver has just entered the fault state.
    pub fn gnss_failed(&mut self) -> bool {
        self.gnss.as_mut().is_some_and(Source::fail)
    }

    pub fn snapshot(&self, now: Instant) -> Snapshot<IMUS> {
        Snapshot {
            imu: self.imu.each_ref().map(|source| source.reading(now)),
            gnss: self.gnss.as_ref().map(|source| source.reading(now)),
        }
    }
}

enum Input {
    Imu(usize, imu::Event),
    Gnss(gnss::Event),
}

pub struct Observation {
    input: Input,
    received: Instant,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SensorFault {
    Imu(usize, imu::DeviceError),
    Gnss(gnss::DeviceError),
}

/// One owner for the sessions and their readings. The caller supplies the
/// inbox, allowing sensor events to share its wait point with other inputs.
/// Session handles are allocated once on open to support heterogeneous devices.
pub struct ObservationService<const IMUS: usize> {
    values: Observations<IMUS>,
    imu: [Option<Box<dyn imu::Session + Send>>; IMUS],
    gnss: Option<Box<dyn gnss::Session + Send>>,
    stopping: bool,
}

impl<const IMUS: usize> ObservationService<IMUS> {
    pub fn new(imu_fresh_for: Duration, gnss_fresh_for: Option<Duration>) -> Self {
        log::info!(
            "configured {} IMU channels, GNSS {}",
            IMUS,
            if gnss_fresh_for.is_some() {
                "enabled"
            } else {
                "disabled"
            }
        );
        Self {
            values: Observations::new(imu_fresh_for, gnss_fresh_for),
            imu: std::array::from_fn(|_| None),
            gnss: None,
            stopping: false,
        }
    }

    pub fn start_imu<E: Send + 'static>(
        &mut self,
        index: usize,
        device: &'static impl imu::Device,
        sender: Sender<E>,
        wrap: fn(Observation) -> E,
    ) -> Result<(), SensorFault> {
        let Some(slot) = self.imu.get_mut(index) else {
            return Err(SensorFault::Imu(index, imu::DeviceError::InvalidData));
        };
        if self.stopping || slot.is_some() || matches!(self.values.imu[index].latest, Latest::Fault)
        {
            return Err(SensorFault::Imu(index, imu::DeviceError::Busy));
        }
        match device.open_default(sink(sender, move |event| Input::Imu(index, event), wrap)) {
            Ok(session) => {
                *slot = Some(Box::new(session));
                Ok(())
            }
            Err(error) => {
                self.values.imu_failed(index);
                Err(SensorFault::Imu(index, error))
            }
        }
    }

    pub fn start_gnss<E: Send + 'static>(
        &mut self,
        device: &'static impl gnss::Device,
        sender: Sender<E>,
        wrap: fn(Observation) -> E,
    ) -> Result<(), SensorFault> {
        let Some(source) = &self.values.gnss else {
            return Err(SensorFault::Gnss(gnss::DeviceError::Unsupported));
        };
        if self.stopping || self.gnss.is_some() || matches!(source.latest, Latest::Fault) {
            return Err(SensorFault::Gnss(gnss::DeviceError::Busy));
        }
        match device.open(sink(sender, Input::Gnss, wrap)) {
            Ok(session) => {
                self.gnss = Some(Box::new(session));
                Ok(())
            }
            Err(error) => {
                self.values.gnss_failed();
                Err(SensorFault::Gnss(error))
            }
        }
    }

    pub fn observe(&mut self, observation: Observation) -> Option<SensorFault> {
        let received = observation.received;
        match observation.input {
            Input::Imu(index, imu::Event::Sample(sample)) => {
                self.values.imu_sample(index, sample, received)
            }
            Input::Gnss(gnss::Event::Fix(fix)) => self.values.gnss_fix(fix, received),
            Input::Gnss(gnss::Event::InvalidFrame) => {}
            Input::Imu(index, imu::Event::Failed(error)) => {
                return self
                    .values
                    .imu_failed(index)
                    .then_some(SensorFault::Imu(index, error));
            }
            Input::Gnss(gnss::Event::Failed(error)) => {
                return self
                    .values
                    .gnss_failed()
                    .then_some(SensorFault::Gnss(error));
            }
        }
        None
    }

    /// Reconcile terminal results before publishing a snapshot, including
    /// faults whose notification could not be admitted to the inbox.
    pub fn reap_finished(&mut self, mut report: impl FnMut(SensorFault)) {
        for (index, slot) in self.imu.iter_mut().enumerate() {
            if slot.as_ref().is_some_and(|session| session.is_finished()) {
                let error = slot
                    .take()
                    .unwrap()
                    .join()
                    .err()
                    .unwrap_or(imu::DeviceError::Io);
                if self.values.imu_failed(index) {
                    report(SensorFault::Imu(index, error));
                }
            }
        }
        if self
            .gnss
            .as_ref()
            .is_some_and(|session| session.is_finished())
        {
            let error = self
                .gnss
                .take()
                .unwrap()
                .join()
                .err()
                .unwrap_or(gnss::DeviceError::Io);
            if self.values.gnss_failed() {
                report(SensorFault::Gnss(error));
            }
        }
    }

    pub fn snapshot(&self, now: Instant) -> Snapshot<IMUS> {
        self.values.snapshot(now)
    }

    /// Samples dropped while the corresponding sessions are active.
    pub fn dropped(&self) -> ([Option<u32>; IMUS], Option<u32>) {
        (
            std::array::from_fn(|index| self.imu[index].as_ref().map(|session| session.dropped())),
            self.gnss.as_ref().map(|session| session.dropped()),
        )
    }

    /// Request cancellation of every session before the caller joins any of its workers.
    pub fn request_stop(&mut self, mut report: impl FnMut(SensorFault)) {
        self.stopping = true;
        for (index, session) in self.imu.iter_mut().enumerate() {
            if let Some(error) = session
                .as_mut()
                .and_then(|session| session.request_stop().err())
            {
                report(SensorFault::Imu(index, error));
            }
        }
        if let Some(error) = self
            .gnss
            .as_mut()
            .and_then(|session| session.request_stop().err())
        {
            report(SensorFault::Gnss(error));
        }
    }

    /// Join every session after cancellation has been requested.
    pub fn join(&mut self, mut report: impl FnMut(SensorFault)) {
        for (index, slot) in self.imu.iter_mut().enumerate() {
            if let Some(error) = slot.take().and_then(|mut session| session.join().err()) {
                report(SensorFault::Imu(index, error));
            }
        }
        if let Some(error) = self
            .gnss
            .take()
            .and_then(|mut session| session.join().err())
        {
            report(SensorFault::Gnss(error));
        }
    }
}

fn sink<T: Copy, E>(
    sender: Sender<E>,
    input: impl Fn(T) -> Input,
    wrap: fn(Observation) -> E,
) -> impl FnMut(T) -> DeliveryResult<T> {
    move |value| {
        let observation = Observation {
            input: input(value),
            received: Instant::now(),
        };
        sender
            .try_send(wrap(observation))
            .map_err(|error| Rejected {
                value,
                error: match error {
                    TrySendError::Full(_) => DeliveryError::Full,
                    TrySendError::Disconnected(_) => DeliveryError::Closed,
                },
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FinishedImu;

    impl imu::Session for FinishedImu {
        fn request_stop(&mut self) -> Result<(), imu::DeviceError> {
            Ok(())
        }

        fn join(&mut self) -> Result<(), imu::DeviceError> {
            Err(imu::DeviceError::Io)
        }

        fn is_finished(&self) -> bool {
            true
        }

        fn dropped(&self) -> u32 {
            1
        }
    }

    #[test]
    fn reaping_recovers_a_terminal_fault_with_no_inbox_capacity() {
        let mut service = ObservationService::<1>::new(Duration::from_millis(500), None);
        service.imu[0] = Some(Box::new(FinishedImu));
        let (sender, inbox) = messaging::bounded(1);
        let mut deliver = sink(sender, |event| Input::Imu(0, event), |event| event);
        let sample = imu::ImuSample {
            sequence: 1,
            timestamp_ms: 0,
            sample_interval_us: 10_000,
            accel_mps2: [0.0; 3],
            gyro_rps: [0.0; 3],
        };
        deliver(imu::Event::Sample(sample)).unwrap();
        let failure = deliver(imu::Event::Failed(imu::DeviceError::Io)).unwrap_err();
        assert_eq!(failure.error, DeliveryError::Full);

        let mut faults = Vec::new();
        service.reap_finished(|fault| faults.push(fault));
        service.reap_finished(|fault| faults.push(fault));
        assert_eq!(faults, [SensorFault::Imu(0, imu::DeviceError::Io)]);
        assert!(service.observe(inbox.try_recv().unwrap()).is_none());
        assert_eq!(service.snapshot(Instant::now()).imu, [Reading::Fault]);
    }

    #[test]
    fn one_imu_can_run_without_gnss_and_faults_survive_late_samples() {
        let freshness = Duration::from_millis(500);
        let time = Instant::now();
        let mut observations = Observations::<1>::new(freshness, None);
        assert_eq!(observations.snapshot(time).gnss, None);
        assert!(!observations.gnss_failed());
        assert_eq!(observations.snapshot(time).imu, [Reading::Waiting]);

        let sample = imu::ImuSample {
            sequence: 4,
            timestamp_ms: 0,
            sample_interval_us: 10_000,
            accel_mps2: [0.0, 0.0, 9.8],
            gyro_rps: [0.0; 3],
        };
        observations.imu_sample(0, sample, time);
        assert_eq!(observations.snapshot(time).imu, [Reading::Live(sample)]);
        assert_eq!(
            observations.snapshot(time + freshness).imu,
            [Reading::Stale]
        );

        assert!(observations.imu_failed(0));
        observations.imu_sample(0, sample, time + freshness);
        assert!(!observations.imu_failed(0));
        assert_eq!(
            observations.snapshot(time + freshness).imu,
            [Reading::Fault]
        );
    }
}
