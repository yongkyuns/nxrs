//! Own configured acquisition sessions and track normalized observations.
//! HALs acquire and deliver samples. The caller dispatches events and deadlines.

use messaging::{DeliveryResult, Sender};
use std::sync::{Arc, Mutex};
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

    fn observe(&mut self, observation: &Observation) -> Option<SensorFault> {
        let received = observation.received;
        match observation.input {
            Input::Imu(index, imu::Event::Sample(sample)) => {
                self.imu_sample(index, sample, received)
            }
            Input::Gnss(gnss::Event::Fix(fix)) => self.gnss_fix(fix, received),
            Input::Gnss(gnss::Event::Satellites(_) | gnss::Event::InvalidFrame) => {}
            Input::Imu(index, imu::Event::Failed(error)) => {
                return self
                    .imu_failed(index)
                    .then_some(SensorFault::Imu(index, error));
            }
            Input::Gnss(gnss::Event::Failed(error)) => {
                return self.gnss_failed().then_some(SensorFault::Gnss(error));
            }
        }
        None
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

/// A sample carried by an observation, with its original acquisition metadata.
#[derive(Clone, Copy)]
pub enum Measurement {
    Imu(usize, imu::ImuSample),
    Gnss(gnss::GnssFix),
}

impl Observation {
    pub fn measurement(&self) -> Option<Measurement> {
        match self.input {
            Input::Imu(index, imu::Event::Sample(sample)) => Some(Measurement::Imu(index, sample)),
            Input::Gnss(gnss::Event::Fix(fix)) => Some(Measurement::Gnss(fix)),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SensorFault {
    Imu(usize, imu::DeviceError),
    Gnss(gnss::DeviceError),
}

/// Shared latest values for consumers with independent deadlines.
#[derive(Clone)]
pub struct SharedObservations<const IMUS: usize>(Arc<Mutex<SharedValues<IMUS>>>);

struct SharedValues<const IMUS: usize> {
    observations: Observations<IMUS>,
    satellites: Source<gnss::SatelliteReport>,
}

impl<const IMUS: usize> SharedObservations<IMUS> {
    pub fn snapshot(&self, now: Instant) -> Snapshot<IMUS> {
        self.0.lock().unwrap().observations.snapshot(now)
    }

    /// Expire a report after two receiver intervals, with a six-second minimum.
    pub fn satellites(&self, now: Instant) -> Reading<gnss::SatelliteReport> {
        let values = self.0.lock().unwrap();
        match &values.observations.gnss {
            Some(source) if matches!(source.latest, Latest::Fault) => Reading::Fault,
            Some(_) => values.satellites.reading(now),
            None => Reading::Waiting,
        }
    }

    /// Publish an observation before notifying any consumer. The first fault
    /// is returned as a wake hint; the snapshot retains the terminal state.
    pub fn record(&self, observation: &Observation) -> Result<Option<Measurement>, SensorFault> {
        let mut values = self.0.lock().unwrap();
        if let Input::Gnss(gnss::Event::Satellites(value)) = observation.input {
            values.satellites.fresh_for =
                Duration::from_millis(2 * u64::from(value.interval_ms.max(3_000)));
            values.satellites.receive(value, observation.received);
            return Ok(None);
        }
        if let Some(fault) = values.observations.observe(observation) {
            return Err(fault);
        }
        Ok(observation.measurement())
    }
}

enum Values<const IMUS: usize> {
    Local(Observations<IMUS>),
    Shared(SharedObservations<IMUS>),
}

/// One owner for the sessions and their readings. The caller supplies the
/// inbox, allowing sensor events to share its wait point with other inputs.
/// Session handles are allocated once on open to support heterogeneous devices.
pub struct ObservationService<const IMUS: usize> {
    values: Values<IMUS>,
    imu: [Option<Box<dyn imu::Session + Send>>; IMUS],
    gnss: Option<Box<dyn gnss::Session + Send>>,
    stopping: bool,
}

impl<const IMUS: usize> ObservationService<IMUS> {
    pub fn new(imu_fresh_for: Duration, gnss_fresh_for: Option<Duration>) -> Self {
        Self::with_values(Values::Local(Observations::new(
            imu_fresh_for,
            gnss_fresh_for,
        )))
    }

    pub fn new_shared(
        imu_fresh_for: Duration,
        gnss_fresh_for: Option<Duration>,
    ) -> (Self, SharedObservations<IMUS>) {
        let shared = SharedObservations(Arc::new(Mutex::new(SharedValues {
            observations: Observations::new(imu_fresh_for, gnss_fresh_for),
            satellites: Source::new(Duration::ZERO),
        })));
        (Self::with_values(Values::Shared(shared.clone())), shared)
    }

    fn with_values(values: Values<IMUS>) -> Self {
        let gnss_configured = match &values {
            Values::Local(values) => values.gnss.is_some(),
            Values::Shared(shared) => shared.0.lock().unwrap().observations.gnss.is_some(),
        };
        crate::diagnostics::configured(IMUS, gnss_configured);
        Self {
            values,
            imu: std::array::from_fn(|_| None),
            gnss: None,
            stopping: false,
        }
    }

    fn update<R>(&mut self, update: impl FnOnce(&mut Observations<IMUS>) -> R) -> R {
        match &mut self.values {
            Values::Local(values) => update(values),
            Values::Shared(shared) => update(&mut shared.0.lock().unwrap().observations),
        }
    }

    pub fn start_imu<E: Send + 'static>(
        &mut self,
        index: usize,
        device: &'static impl imu::Device,
        sender: Sender<E>,
        mut wrap: impl FnMut(Observation) -> E + Send + 'static,
    ) -> Result<(), SensorFault> {
        self.start_imu_with(index, device, sender, move |event| Some(wrap(event)))
    }

    /// The callback runs on the acquisition task and can own its publication
    /// endpoint. Return `None` after publishing to skip the inbox.
    pub fn start_imu_with<E: Send + 'static>(
        &mut self,
        index: usize,
        device: &'static impl imu::Device,
        sender: Sender<E>,
        wrap: impl FnMut(Observation) -> Option<E> + Send + 'static,
    ) -> Result<(), SensorFault> {
        let Some(slot) = self.imu.get(index) else {
            return Err(SensorFault::Imu(index, imu::DeviceError::InvalidData));
        };
        if self.stopping
            || slot.is_some()
            || self.update(|values| matches!(values.imu[index].latest, Latest::Fault))
        {
            return Err(SensorFault::Imu(index, imu::DeviceError::Busy));
        }
        match device.open_default(sink(sender, move |event| Input::Imu(index, event), wrap)) {
            Ok(session) => {
                self.imu[index] = Some(Box::new(session));
                Ok(())
            }
            Err(error) => {
                self.update(|values| values.imu_failed(index));
                Err(SensorFault::Imu(index, error))
            }
        }
    }

    pub fn start_gnss<E: Send + 'static>(
        &mut self,
        device: &'static impl gnss::Device,
        sender: Sender<E>,
        mut wrap: impl FnMut(Observation) -> E + Send + 'static,
    ) -> Result<(), SensorFault> {
        self.start_gnss_with(device, sender, move |event| Some(wrap(event)))
    }

    /// The callback runs on the acquisition task and can own its publication
    /// endpoint. Return `None` after publishing to skip the inbox.
    pub fn start_gnss_with<E: Send + 'static>(
        &mut self,
        device: &'static impl gnss::Device,
        sender: Sender<E>,
        wrap: impl FnMut(Observation) -> Option<E> + Send + 'static,
    ) -> Result<(), SensorFault> {
        let Some(failed) = self.update(|values| {
            values
                .gnss
                .as_ref()
                .map(|source| matches!(source.latest, Latest::Fault))
        }) else {
            return Err(SensorFault::Gnss(gnss::DeviceError::Unsupported));
        };
        if self.stopping || self.gnss.is_some() || failed {
            return Err(SensorFault::Gnss(gnss::DeviceError::Busy));
        }
        match device.open(sink(sender, Input::Gnss, wrap)) {
            Ok(session) => {
                self.gnss = Some(Box::new(session));
                Ok(())
            }
            Err(error) => {
                self.update(Observations::gnss_failed);
                Err(SensorFault::Gnss(error))
            }
        }
    }

    pub fn observe(&mut self, observation: Observation) -> Option<SensorFault> {
        match &mut self.values {
            Values::Local(values) => values.observe(&observation),
            Values::Shared(shared) => shared.record(&observation).err(),
        }
    }

    /// Reconcile terminal results before publishing a snapshot, including
    /// faults whose notification could not be admitted to the inbox.
    pub fn reap_finished(&mut self, mut report: impl FnMut(SensorFault)) {
        // Shared state is published by the sink. Report the joined result
        // once even if the display's wake notification was lost.
        let shared = matches!(self.values, Values::Shared(_));
        for index in 0..IMUS {
            let slot = &mut self.imu[index];
            if slot.as_ref().is_some_and(|session| session.is_finished()) {
                let error = slot
                    .take()
                    .unwrap()
                    .join()
                    .err()
                    .unwrap_or(imu::DeviceError::Io);
                if self.update(|values| values.imu_failed(index)) || shared {
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
            if self.update(Observations::gnss_failed) || shared {
                report(SensorFault::Gnss(error));
            }
        }
    }

    pub fn snapshot(&self, now: Instant) -> Snapshot<IMUS> {
        match &self.values {
            Values::Local(values) => values.snapshot(now),
            Values::Shared(shared) => shared.snapshot(now),
        }
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
    mut wrap: impl FnMut(Observation) -> Option<E> + Send + 'static,
) -> impl FnMut(T) -> DeliveryResult<T> {
    crate::inbox_sink(sender, move |value| {
        wrap(Observation {
            input: input(value),
            received: Instant::now(),
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use messaging::DeliveryError;

    #[test]
    fn observation_callback_can_own_mutable_state() {
        let (sender, inbox) = messaging::bounded(1);
        let mut calls = 0;
        let mut deliver = sink(sender, Input::Gnss, move |event| {
            calls += 1;
            (calls == 2).then_some(event)
        });
        deliver(gnss::Event::InvalidFrame).unwrap();
        assert!(matches!(
            inbox.try_recv(),
            Err(std::sync::mpsc::TryRecvError::Empty)
        ));
        deliver(gnss::Event::Failed(gnss::DeviceError::Io)).unwrap();
        let delivered = inbox.try_recv().unwrap();
        assert!(matches!(
            delivered.input,
            Input::Gnss(gnss::Event::Failed(gnss::DeviceError::Io))
        ));
    }

    #[test]
    fn shared_satellites_remain_live_across_a_slow_receiver_interval() {
        let (mut service, readings) =
            ObservationService::<0>::new_shared(Duration::ZERO, Some(Duration::from_secs(3)));
        let now = Instant::now();
        let report = gnss::SatelliteReport::new(4, 0, None, 10_000, 0, []);
        assert!(service
            .observe(Observation {
                input: Input::Gnss(gnss::Event::Satellites(report)),
                received: now,
            })
            .is_none());
        assert_eq!(readings.snapshot(now).gnss, Some(Reading::Waiting));
        assert_eq!(
            readings.satellites(now + Duration::from_secs(10)),
            Reading::Live(report)
        );
        assert_eq!(
            readings.satellites(now + Duration::from_secs(20)),
            Reading::Stale
        );
        service.observe(Observation {
            input: Input::Gnss(gnss::Event::Failed(gnss::DeviceError::Io)),
            received: now,
        });
        assert_eq!(readings.satellites(now), Reading::Fault);
    }

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
        let mut deliver = sink(sender, |event| Input::Imu(0, event), Some);
        let sample = imu::ImuSample {
            sequence: 1,
            timestamp_us: 0,
            is_capture_time: false,
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
    fn shared_samples_are_visible_without_dispatching_the_owner_inbox() {
        let (service, readings) =
            ObservationService::<1>::new_shared(Duration::from_millis(500), None);
        let sink_readings = readings.clone();
        let (sender, inbox) = messaging::bounded(1);
        let pending = sender.clone();
        let mut deliver = sink(
            sender,
            |event| Input::Imu(0, event),
            move |event| sink_readings.record(&event).is_err().then_some(event),
        );
        let sample = imu::ImuSample {
            sequence: 1,
            timestamp_us: 0,
            is_capture_time: false,
            sample_interval_us: 10_000,
            accel_mps2: [0.0; 3],
            gyro_rps: [0.0; 3],
        };
        deliver(imu::Event::Sample(sample)).unwrap();
        assert_eq!(
            readings.snapshot(Instant::now()).imu,
            [Reading::Live(sample)]
        );
        assert!(matches!(
            inbox.try_recv(),
            Err(std::sync::mpsc::TryRecvError::Empty)
        ));
        // Fill the display inbox just before the fault arrives.
        pending
            .try_send(Observation {
                input: Input::Gnss(gnss::Event::InvalidFrame),
                received: Instant::now(),
            })
            .ok()
            .unwrap();
        let fault = deliver(imu::Event::Failed(imu::DeviceError::Io)).unwrap_err();
        assert_eq!(fault.error, DeliveryError::Full);
        assert_eq!(service.snapshot(Instant::now()).imu, [Reading::Fault]);
        deliver(imu::Event::Sample(sample)).unwrap();
        assert_eq!(readings.snapshot(Instant::now()).imu, [Reading::Fault]);
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
            timestamp_us: 0,
            is_capture_time: false,
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
