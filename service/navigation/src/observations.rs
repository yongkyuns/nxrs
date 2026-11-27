//! Own configured acquisition sessions and track normalized observations.
//! HALs acquire and deliver samples. The caller dispatches events and deadlines.

use messaging::{DeliveryResult, Sender};
use std::time::{Duration, Instant};

mod fixed;
pub use fixed::{fixed_readers, FixedReader, Publisher};

/// Current availability, separately from historical last measurements.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Reading<T> {
    Waiting,
    /// Capture age is known and within the configured freshness budget.
    Live(T),
    /// Received recently, but capture age is not established by this provider.
    Received(T),
    Stale,
    /// Explicitly no current solution, or contradictory timing metadata.
    Unavailable,
    Fault,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Availability {
    Waiting,
    Available,
    Unavailable,
    Fault,
}

#[derive(Clone, Copy)]
enum SampleTiming {
    Capture(Duration),
    Receipt,
    Invalid,
}

fn sample_timing(timestamp_us: u64, received_us: u64, is_capture_time: bool) -> SampleTiming {
    if is_capture_time {
        received_us
            .checked_sub(timestamp_us)
            .map_or(SampleTiming::Invalid, |age| {
                SampleTiming::Capture(Duration::from_micros(age))
            })
    } else {
        SampleTiming::Receipt
    }
}

#[derive(Clone, Copy)]
struct Source<T> {
    last: Option<T>,
    availability: Availability,
    observed: Instant,
    timing: SampleTiming,
    fresh_for: Duration,
}

impl<T: Copy> Source<T> {
    fn new(fresh_for: Duration) -> Self {
        Self {
            last: None,
            availability: Availability::Waiting,
            observed: Instant::now(),
            timing: SampleTiming::Receipt,
            fresh_for,
        }
    }

    fn receive(&mut self, value: T, received: Instant, timing: SampleTiming) {
        if self.availability != Availability::Fault {
            if !matches!(timing, SampleTiming::Invalid) {
                self.last = Some(value);
            }
            self.observed = received;
            self.timing = timing;
            self.availability = if matches!(timing, SampleTiming::Invalid) {
                Availability::Unavailable
            } else {
                Availability::Available
            };
        }
    }

    fn unavailable(&mut self, received: Instant) {
        if self.availability != Availability::Fault {
            self.availability = Availability::Unavailable;
            self.observed = received;
        }
    }

    fn fail(&mut self) -> bool {
        let first = self.availability != Availability::Fault;
        self.availability = Availability::Fault;
        first
    }

    fn reading(&self, now: Instant) -> Reading<T> {
        let elapsed = now.saturating_duration_since(self.observed);
        match self.availability {
            Availability::Waiting if elapsed < self.fresh_for => Reading::Waiting,
            Availability::Waiting => Reading::Stale,
            Availability::Available if elapsed >= self.fresh_for => Reading::Stale,
            Availability::Available => match (self.last, self.timing) {
                (Some(value), SampleTiming::Capture(age))
                    if age
                        .checked_add(elapsed)
                        .is_some_and(|age| age < self.fresh_for) =>
                {
                    Reading::Live(value)
                }
                (Some(value), SampleTiming::Receipt) => Reading::Received(value),
                (_, SampleTiming::Invalid) => Reading::Unavailable,
                _ => Reading::Stale,
            },
            Availability::Unavailable if elapsed < self.fresh_for => Reading::Unavailable,
            Availability::Unavailable => Reading::Stale,
            Availability::Fault => Reading::Fault,
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
    /// report starts at `Waiting`, expires to `Stale`, and reports explicit
    /// no-fix as `Unavailable`.
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
        self.imu[index].receive(
            sample,
            received,
            sample_timing(
                sample.timestamp_us,
                sample.received_us,
                sample.is_capture_time,
            ),
        );
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
            source.receive(
                fix,
                received,
                sample_timing(fix.timestamp_us, fix.received_us, fix.is_capture_time),
            );
        }
    }

    /// Explicit no-fix invalidates current position immediately. The previous
    /// accepted position remains available only through `last_gnss`.
    pub fn gnss_unavailable(&mut self, received: Instant) {
        if let Some(source) = &mut self.gnss {
            source.unavailable(received);
        }
    }

    pub fn last_gnss(&self) -> Option<gnss::GnssFix> {
        self.gnss.as_ref().and_then(|source| source.last)
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
            Input::Gnss(gnss::Event::NoFix(_)) => self.gnss_unavailable(received),
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
    /// Explicit receiver unavailability, retained independently of sample export.
    pub fn no_fix(&self) -> Option<gnss::NoFix> {
        match self.input {
            Input::Gnss(gnss::Event::NoFix(value)) => Some(value),
            _ => None,
        }
    }

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

/// One owner for the sessions and their readings. The caller supplies the
/// inbox, allowing sensor events to share its wait point with other inputs.
/// Session handles are allocated once on open to support heterogeneous devices.
pub struct ObservationService<const IMUS: usize> {
    values: Observations<IMUS>,
    imu: [Option<Box<dyn imu::Session + Send>>; IMUS],
    imu_failures: [Option<imu::Failure>; IMUS],
    gnss: Option<Box<dyn gnss::Session + Send>>,
    stopping: bool,
}

impl<const IMUS: usize> ObservationService<IMUS> {
    pub fn new(imu_fresh_for: Duration, gnss_fresh_for: Option<Duration>) -> Self {
        crate::diagnostics::configured(IMUS, gnss_fresh_for.is_some());
        Self {
            values: Observations::new(imu_fresh_for, gnss_fresh_for),
            imu: std::array::from_fn(|_| None),
            imu_failures: [None; IMUS],
            gnss: None,
            stopping: false,
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
            || self.values.imu[index].availability == Availability::Fault
        {
            return Err(SensorFault::Imu(index, imu::DeviceError::Busy));
        }
        match device.open_default(sink(sender, move |event| Input::Imu(index, event), wrap)) {
            Ok(session) => {
                self.imu[index] = Some(Box::new(session));
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
        let Some(failed) = self
            .values
            .gnss
            .as_ref()
            .map(|source| source.availability == Availability::Fault)
        else {
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
                self.values.gnss_failed();
                Err(SensorFault::Gnss(error))
            }
        }
    }

    pub fn observe(&mut self, observation: Observation) -> Option<SensorFault> {
        self.values.observe(&observation)
    }

    /// Reconcile terminal results before publishing a snapshot, including
    /// faults whose notification could not be admitted to the inbox.
    pub fn reap_finished(&mut self, mut report: impl FnMut(SensorFault)) {
        for index in 0..IMUS {
            let slot = &mut self.imu[index];
            if slot.as_ref().is_some_and(|session| session.is_finished()) {
                let mut session = slot.take().unwrap();
                let result = session.join();
                let failure = session.failure();
                self.imu_failures[index] = failure;
                if result.is_err() || !self.stopping {
                    let error = failure
                        .and_then(|failure| failure.acquisition.or(failure.cleanup))
                        .or(result.err())
                        .unwrap_or(imu::DeviceError::Io);
                    if self.values.imu_failed(index) {
                        report(SensorFault::Imu(index, error));
                    }
                }
            }
        }
        if self
            .gnss
            .as_ref()
            .is_some_and(|session| session.is_finished())
        {
            let result = self.gnss.take().unwrap().join();
            if result.is_err() || !self.stopping {
                let error = result.err().unwrap_or(gnss::DeviceError::Io);
                if self.values.gnss_failed() {
                    report(SensorFault::Gnss(error));
                }
            }
        }
    }

    pub fn snapshot(&self, now: Instant) -> Snapshot<IMUS> {
        self.values.snapshot(now)
    }

    /// Joined terminal evidence, retained after session handles are retired.
    /// Acquisition and cleanup can fail independently. `None` means no structured
    /// failure is available, including providers exposing only a legacy error.
    pub fn imu_failures(&self) -> [Option<imu::Failure>; IMUS] {
        self.imu_failures
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

    /// Whether all admitted sessions have been joined and their handles retired.
    /// A failed wake or an error return from `join` alone is not quiescence.
    pub fn is_quiescent(&self) -> bool {
        self.imu.iter().all(Option::is_none) && self.gnss.is_none()
    }

    /// Whether any admitted session still requires joined retirement.
    pub fn shutdown_pending(&self) -> bool {
        !self.is_quiescent()
    }

    /// Join after cancellation has been requested. Failed joins retain running
    /// sessions for another cancellation/join attempt. The caller must retain
    /// supervision until `is_quiescent` is true.
    pub fn join(&mut self, mut report: impl FnMut(SensorFault)) {
        for (index, slot) in self.imu.iter_mut().enumerate() {
            let Some(session) = slot.as_mut() else {
                continue;
            };
            let result = session.join();
            let finished = session.is_finished();
            if finished {
                self.imu_failures[index] = session.failure();
            }
            if let Err(error) = result {
                report(SensorFault::Imu(index, error));
            } else if !finished {
                report(SensorFault::Imu(index, imu::DeviceError::Io));
            }
            if finished {
                *slot = None;
            }
        }
        if let Some(session) = &mut self.gnss {
            let result = session.join();
            let finished = session.is_finished();
            if let Err(error) = result {
                report(SensorFault::Gnss(error));
            } else if !finished {
                report(SensorFault::Gnss(gnss::DeviceError::Io));
            }
            if finished {
                self.gnss = None;
            }
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
    fn fixed_satellites_retain_the_receivers_interval_and_terminal_state() {
        let (_, mut writer, [mut display, mut links]) =
            fixed_readers::<0>(Duration::ZERO, Duration::from_secs(3));
        let now = Instant::now();
        let report = gnss::SatelliteReport::new(4, 0, None, 10_000, 0, []);
        writer
            .record(&Observation {
                input: Input::Gnss(gnss::Event::Satellites(report)),
                received: now,
            })
            .unwrap();
        assert_eq!(links.snapshot(now).gnss, Some(Reading::Waiting));
        assert_eq!(
            display.satellites(now + Duration::from_secs(19)),
            Reading::Received(report)
        );
        assert_eq!(
            display.satellites(now + Duration::from_secs(20)),
            Reading::Stale
        );
        display.fail(SensorFault::Gnss(gnss::DeviceError::Io));
        assert_eq!(links.snapshot(now).gnss, Some(Reading::Fault));
        assert_eq!(display.satellites(now), Reading::Fault);
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
            received_us: 0,
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
    fn joined_retirement_keeps_acquisition_and_cleanup_causes_independent() {
        struct Failed;
        impl imu::Session for Failed {
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
                0
            }
            fn failure(&self) -> Option<imu::Failure> {
                Some(imu::Failure {
                    acquisition: Some(imu::DeviceError::Timeout),
                    cleanup: Some(imu::DeviceError::Io),
                })
            }
        }
        for explicit_join in [false, true] {
            let mut service = ObservationService::<1>::new(Duration::from_millis(500), None);
            service.imu[0] = Some(Box::new(Failed));
            let mut reported = Vec::new();
            if explicit_join {
                service.request_stop(|_| panic!("cancellation succeeded"));
                service.join(|fault| reported.push(fault));
                assert_eq!(reported, [SensorFault::Imu(0, imu::DeviceError::Io)]);
            } else {
                service.reap_finished(|fault| reported.push(fault));
                assert_eq!(reported, [SensorFault::Imu(0, imu::DeviceError::Timeout)]);
            }
            service.reap_finished(|_| panic!("already joined"));
            assert!(service.is_quiescent());
            assert_eq!(
                service.imu_failures(),
                [Some(imu::Failure {
                    acquisition: Some(imu::DeviceError::Timeout),
                    cleanup: Some(imu::DeviceError::Io),
                })]
            );
        }
    }

    #[test]
    fn fixed_readers_poll_independently_and_faults_survive_a_full_inbox() {
        let ([mut writer], _, [mut display, mut links]) =
            fixed_readers(Duration::from_millis(500), Duration::ZERO);
        let now = Instant::now();
        assert_eq!(display.snapshot(now).imu, [Reading::Waiting]);
        let (sender, inbox) = messaging::bounded(1);
        let pending = sender.clone();
        let mut deliver = sink(
            sender,
            |event| Input::Imu(0, event),
            move |event| writer.record(&event).is_err().then_some(event),
        );
        let sample = imu::ImuSample {
            sequence: 1,
            timestamp_us: 0,
            received_us: 0,
            is_capture_time: false,
            sample_interval_us: 10_000,
            accel_mps2: [0.0; 3],
            gyro_rps: [0.0; 3],
        };
        deliver(imu::Event::Sample(sample)).unwrap();
        assert_eq!(
            display.snapshot(Instant::now()).imu,
            [Reading::Received(sample)]
        );
        let second = imu::ImuSample {
            sequence: 2,
            ..sample
        };
        deliver(imu::Event::Sample(second)).unwrap();
        assert_eq!(
            links.snapshot(Instant::now()).imu,
            [Reading::Received(second)]
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
        assert_eq!(display.snapshot(Instant::now()).imu, [Reading::Fault]);
        deliver(imu::Event::Sample(sample)).unwrap();
        assert_eq!(links.snapshot(Instant::now()).imu, [Reading::Fault]);
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
            received_us: 0,
            is_capture_time: false,
            sample_interval_us: 10_000,
            accel_mps2: [0.0, 0.0, 9.8],
            gyro_rps: [0.0; 3],
        };
        observations.imu_sample(0, sample, time);
        assert_eq!(observations.snapshot(time).imu, [Reading::Received(sample)]);
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

    fn fix(timestamp_us: u64, received_us: u64, is_capture_time: bool) -> gnss::GnssFix {
        gnss::GnssFix {
            generation: 1,
            sequence: 1,
            source_epoch_ms: 100,
            timestamp_us,
            received_us,
            is_capture_time,
            receiver_utc_ms: None,
            latitude_deg: 48.0,
            longitude_deg: 11.0,
            speed_mps: 0.0,
            course_rad: None,
            quality: None,
        }
    }

    #[test]
    fn first_progress_and_capture_age_use_the_declared_freshness_budget() {
        let budget = Duration::from_millis(500);
        let mut observations = Observations::<1>::new(budget, Some(budget));
        let now = Instant::now();
        assert_eq!(observations.snapshot(now).imu, [Reading::Waiting]);
        assert_eq!(observations.snapshot(now + budget).imu, [Reading::Stale]);
        assert_eq!(
            observations.snapshot(now + budget).gnss,
            Some(Reading::Stale)
        );

        let captured = fix(100_000, 400_000, true);
        observations.gnss_fix(captured, now);
        assert_eq!(
            observations.snapshot(now).gnss,
            Some(Reading::Live(captured))
        );
        assert_eq!(
            observations.snapshot(now + Duration::from_millis(200)).gnss,
            Some(Reading::Stale)
        );
        // Fresh receipt cannot erase 500 ms already spent before publication.
        observations.gnss_fix(fix(100_000, 600_000, true), now);
        assert_eq!(observations.snapshot(now).gnss, Some(Reading::Stale));

        let unqualified = fix(600_000, 600_000, false);
        observations.gnss_fix(unqualified, now);
        assert_eq!(
            observations.snapshot(now).gnss,
            Some(Reading::Received(unqualified))
        );
        assert_eq!(
            observations.snapshot(now + budget).gnss,
            Some(Reading::Stale)
        );
        observations.gnss_fix(fix(600_001, 600_000, true), now);
        assert_eq!(observations.snapshot(now).gnss, Some(Reading::Unavailable));
        assert_eq!(observations.last_gnss(), Some(unqualified));
    }

    #[test]
    fn no_fix_is_current_unavailability_with_separate_historical_position() {
        let budget = Duration::from_secs(3);
        let now = Instant::now();
        let (_, mut writer, [mut display, mut links]) = fixed_readers::<0>(Duration::ZERO, budget);
        let previous = fix(100, 100, true);
        writer
            .record(&Observation {
                input: Input::Gnss(gnss::Event::Fix(previous)),
                received: now,
            })
            .unwrap();
        assert_eq!(display.snapshot(now).gnss, Some(Reading::Live(previous)));
        let missing = gnss::NoFix {
            generation: 1,
            source_epoch_ms: Some(101),
            received_us: 101,
        };
        let no_fix = Observation {
            input: Input::Gnss(gnss::Event::NoFix(missing)),
            received: now,
        };
        assert_eq!(no_fix.no_fix(), Some(missing));
        assert!(writer.record(&no_fix).unwrap().is_none());
        for reader in [&mut display, &mut links] {
            assert_eq!(reader.snapshot(now).gnss, Some(Reading::Unavailable));
            assert_eq!(reader.last_gnss(), Some(previous));
            // Silence following no-fix is a stale receiver, not perpetual progress.
            assert_eq!(reader.snapshot(now + budget).gnss, Some(Reading::Stale));
        }
        let recovered = gnss::GnssFix {
            sequence: 2,
            source_epoch_ms: 102,
            ..previous
        };
        writer
            .record(&Observation {
                input: Input::Gnss(gnss::Event::Fix(recovered)),
                received: now + budget,
            })
            .unwrap();
        assert_eq!(
            display.snapshot(now + budget).gnss,
            Some(Reading::Live(recovered))
        );
    }

    #[test]
    fn no_fix_and_first_failure_evidence_do_not_depend_on_inbox_capacity() {
        let (_, mut writer, [mut display, links]) =
            fixed_readers::<0>(Duration::ZERO, Duration::from_secs(3));
        let (sender, inbox) = messaging::bounded(1);
        sender.try_send(()).unwrap();
        let mut deliver = sink(sender, Input::Gnss, move |event| {
            writer.record(&event).is_err().then_some(())
        });
        let previous = fix(100, 100, true);
        deliver(gnss::Event::Fix(previous)).unwrap();
        deliver(gnss::Event::NoFix(gnss::NoFix {
            generation: 1,
            source_epoch_ms: Some(101),
            received_us: 101,
        }))
        .unwrap();
        assert_eq!(
            display.snapshot(Instant::now()).gnss,
            Some(Reading::Unavailable)
        );
        assert_eq!(display.last_gnss(), Some(previous));
        deliver(gnss::Event::InvalidFrame).unwrap();
        deliver(gnss::Event::InvalidFrame).unwrap();
        assert_eq!(links.gnss_invalid_frames(), 2);
        let rejected = deliver(gnss::Event::Failed(gnss::DeviceError::Timeout)).unwrap_err();
        assert_eq!(rejected.error, DeliveryError::Full);
        assert_eq!(links.faults(), ([], Some(gnss::DeviceError::Timeout)));
        deliver(gnss::Event::Failed(gnss::DeviceError::Io)).unwrap();
        deliver(gnss::Event::Fix(gnss::GnssFix {
            sequence: 2,
            ..previous
        }))
        .unwrap();
        assert_eq!(display.snapshot(Instant::now()).gnss, Some(Reading::Fault));
        assert_eq!(display.last_gnss(), Some(previous));
        assert_eq!(display.faults(), ([], Some(gnss::DeviceError::Timeout)));
        assert_eq!(inbox.try_recv(), Ok(()));
    }

    struct FailedWake {
        attempts: u8,
        joins: std::sync::Arc<std::sync::atomic::AtomicUsize>,
    }

    macro_rules! failed_wake_session {
        ($capability:ident) => {
            impl $capability::Session for FailedWake {
                fn request_stop(&mut self) -> Result<(), $capability::DeviceError> {
                    self.attempts += 1;
                    if self.attempts == 1 {
                        Err($capability::DeviceError::Io)
                    } else {
                        Ok(())
                    }
                }
                fn join(&mut self) -> Result<(), $capability::DeviceError> {
                    self.joins
                        .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    if self.attempts < 2 {
                        Err($capability::DeviceError::Io)
                    } else {
                        Ok(())
                    }
                }
                fn is_finished(&self) -> bool {
                    self.attempts >= 2
                }
                fn dropped(&self) -> u32 {
                    0
                }
            }
        };
    }
    failed_wake_session!(imu);
    failed_wake_session!(gnss);

    #[test]
    fn failed_cancel_and_join_retain_both_handles_until_successful_retirement() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        let joins = Arc::new(AtomicUsize::new(0));
        let mut service =
            ObservationService::<1>::new(Duration::from_secs(1), Some(Duration::from_secs(3)));
        service.imu[0] = Some(Box::new(FailedWake {
            attempts: 0,
            joins: joins.clone(),
        }));
        service.gnss = Some(Box::new(FailedWake {
            attempts: 0,
            joins: joins.clone(),
        }));
        let mut faults = Vec::new();
        service.request_stop(|fault| faults.push(fault));
        service.join(|fault| faults.push(fault));
        assert_eq!(faults.len(), 4);
        assert!(service.shutdown_pending());
        assert!(!service.is_quiescent());
        assert_eq!(service.dropped(), ([Some(0)], Some(0)));
        service.reap_finished(|_| panic!("running sessions cannot be reaped"));
        assert_eq!(joins.load(Ordering::Relaxed), 2);

        service.request_stop(|_| panic!("retry should wake both sessions"));
        service.reap_finished(|_| panic!("requested successful completion is clean"));
        assert!(service.is_quiescent());
        assert!(!service.shutdown_pending());
        assert_eq!(joins.load(Ordering::Relaxed), 4);
        assert_eq!(service.dropped(), ([None], None));
    }

    #[test]
    fn unexpected_successful_worker_exit_is_still_a_fault() {
        let mut service = ObservationService::<1>::new(Duration::from_secs(1), None);
        service.imu[0] = Some(Box::new(FailedWake {
            attempts: 2,
            joins: Default::default(),
        }));
        let mut faults = Vec::new();
        service.reap_finished(|fault| faults.push(fault));
        assert_eq!(faults, [SensorFault::Imu(0, imu::DeviceError::Io)]);
        assert!(service.is_quiescent());
        assert_eq!(service.snapshot(Instant::now()).imu, [Reading::Fault]);
    }
}
