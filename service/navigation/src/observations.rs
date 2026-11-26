//! Own configured acquisition sessions and track normalized observations.
//! HALs acquire and deliver samples. The caller dispatches events and deadlines.

use messaging::{latest, DeliveryResult, Sender};
use std::marker::PhantomData;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
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
    /// An IMU event with its original reception time and configured ordinal.
    pub fn imu(index: usize, event: imu::Event, received: Instant) -> Self {
        Self {
            input: Input::Imu(index, event),
            received,
        }
    }

    /// A receiver event with its original reception time.
    pub fn gnss(event: gnss::Event, received: Instant) -> Self {
        Self {
            input: Input::Gnss(event),
            received,
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

#[derive(Clone, Copy)]
struct Received<T> {
    value: T,
    at: Instant,
}

struct Faults<const IMUS: usize> {
    imu: [AtomicBool; IMUS],
    gnss: AtomicBool,
}

impl<const IMUS: usize> Faults<IMUS> {
    fn imu_failed(&self, index: usize) -> bool {
        !self.imu[index].swap(true, Ordering::AcqRel)
    }

    fn gnss_failed(&self) -> bool {
        !self.gnss.swap(true, Ordering::AcqRel)
    }
}

/// Exclusive publication endpoints for one acquisition lifetime. Move each
/// endpoint into the corresponding acquisition callback before opening it.
pub struct ObservationPublishers<const IMUS: usize, const READERS: usize> {
    pub imu: [ImuPublisher<IMUS, READERS>; IMUS],
    pub gnss: Option<GnssPublisher<IMUS, READERS>>,
}

/// The only sample writer for one configured IMU ordinal.
pub struct ImuPublisher<const IMUS: usize, const READERS: usize> {
    index: usize,
    samples: latest::Publisher<Option<Received<imu::ImuSample>>, READERS>,
    faults: Arc<Faults<IMUS>>,
}

impl<const IMUS: usize, const READERS: usize> ImuPublisher<IMUS, READERS> {
    /// Publish before notifying a consumer. The first fault is a wake hint;
    /// all readers retain the terminal state independently of inbox capacity.
    /// A late sample remains available to an ordered capture consumer but does
    /// not replace the faulted latest state.
    ///
    /// # Panics
    ///
    /// Panics if the observation belongs to another configured source.
    pub fn record(
        &mut self,
        observation: &Observation,
    ) -> Result<Option<Measurement>, SensorFault> {
        let Input::Imu(index, event) = observation.input else {
            panic!("IMU publisher received a GNSS observation");
        };
        assert_eq!(index, self.index, "IMU publisher ordinal mismatch");
        match event {
            imu::Event::Sample(value) => {
                if !self.faults.imu[index].load(Ordering::Acquire) {
                    self.samples.publish(Some(Received {
                        value,
                        at: observation.received,
                    }));
                }
                Ok(Some(Measurement::Imu(index, value)))
            }
            imu::Event::Failed(error) if self.faults.imu_failed(index) => {
                Err(SensorFault::Imu(index, error))
            }
            imu::Event::Failed(_) => Ok(None),
        }
    }
}

/// The only fix and satellite writers for the configured GNSS receiver.
pub struct GnssPublisher<const IMUS: usize, const READERS: usize> {
    fixes: latest::Publisher<Option<Received<gnss::GnssFix>>, READERS>,
    satellites: latest::Publisher<Option<Received<gnss::SatelliteReport>>, READERS>,
    faults: Arc<Faults<IMUS>>,
}

impl<const IMUS: usize, const READERS: usize> GnssPublisher<IMUS, READERS> {
    /// Publish a receiver observation before a wake hint. Fixes and satellite
    /// reports have independent latest values; neither implies the other is new.
    ///
    /// # Panics
    ///
    /// Panics if the observation belongs to an IMU.
    pub fn record(
        &mut self,
        observation: &Observation,
    ) -> Result<Option<Measurement>, SensorFault> {
        let Input::Gnss(event) = observation.input else {
            panic!("GNSS publisher received an IMU observation");
        };
        match event {
            gnss::Event::Fix(value) => {
                if !self.faults.gnss.load(Ordering::Acquire) {
                    self.fixes.publish(Some(Received {
                        value,
                        at: observation.received,
                    }));
                }
                Ok(Some(Measurement::Gnss(value)))
            }
            gnss::Event::Satellites(value) => {
                if !self.faults.gnss.load(Ordering::Acquire) {
                    self.satellites.publish(Some(Received {
                        value,
                        at: observation.received,
                    }));
                }
                Ok(None)
            }
            gnss::Event::Failed(error) if self.faults.gnss_failed() => {
                Err(SensorFault::Gnss(error))
            }
            gnss::Event::Failed(_) | gnss::Event::InvalidFrame => Ok(None),
        }
    }
}

struct GnssReader {
    fixes: latest::Reader<Option<Received<gnss::GnssFix>>>,
    satellites: latest::Reader<Option<Received<gnss::SatelliteReport>>>,
    fresh_for: Duration,
}

/// One independently scheduled consumer. Readers are neither cloneable nor
/// shareable; move a reader to its owner and read it through mutable access.
/// Samples may coalesce. A snapshot contains complete per-source values, not
/// an atomic transaction across sources or readers.
pub struct ObservationReader<const IMUS: usize> {
    imu: [latest::Reader<Option<Received<imu::ImuSample>>>; IMUS],
    imu_fresh_for: Duration,
    gnss: Option<GnssReader>,
    faults: Arc<Faults<IMUS>>,
}

impl<const IMUS: usize> ObservationReader<IMUS> {
    pub fn snapshot(&mut self, now: Instant) -> Snapshot<IMUS> {
        let imu = std::array::from_fn(|index| {
            let sample = self.imu[index].read();
            if self.faults.imu[index].load(Ordering::Acquire) {
                Reading::Fault
            } else {
                reading(sample, self.imu_fresh_for, now)
            }
        });
        let gnss = self.gnss.as_mut().map(|source| {
            let fix = source.fixes.read();
            if self.faults.gnss.load(Ordering::Acquire) {
                Reading::Fault
            } else {
                reading(fix, source.fresh_for, now)
            }
        });
        Snapshot { imu, gnss }
    }

    /// Expire a report after two receiver intervals, with a six-second minimum.
    pub fn satellites(&mut self, now: Instant) -> Reading<gnss::SatelliteReport> {
        let Some(source) = &mut self.gnss else {
            return Reading::Waiting;
        };
        let report = source.satellites.read();
        if self.faults.gnss.load(Ordering::Acquire) {
            return Reading::Fault;
        }
        let freshness = report.map_or(Duration::ZERO, |report| {
            Duration::from_millis(2 * u64::from(report.value.interval_ms.max(3_000)))
        });
        reading(report, freshness, now)
    }
}

fn reading<T: Copy>(sample: Option<Received<T>>, fresh_for: Duration, now: Instant) -> Reading<T> {
    match sample {
        None => Reading::Waiting,
        Some(sample) if now.saturating_duration_since(sample.at) < fresh_for => {
            Reading::Live(sample.value)
        }
        Some(_) => Reading::Stale,
    }
}

/// Allocate all publication endpoints before starting acquisition. There is one
/// writer per source and a fixed reader set, with three slots per source/reader
/// pair. A configured GNSS receiver has separate fix and satellite pairs.
/// Sticky faults use one separately allocated atomic flag set so open failure
/// and joined terminal reconciliation do not need a second payload writer.
/// Publishing and reading add no allocation, mutex, worker or notification.
/// Dropping an endpoint does not signal a fault or replace joined shutdown.
///
/// # Panics
///
/// Panics if `READERS` is zero.
pub fn publication<const IMUS: usize, const READERS: usize>(
    imu_fresh_for: Duration,
    gnss_fresh_for: Option<Duration>,
) -> (
    ObservationPublishers<IMUS, READERS>,
    [ObservationReader<IMUS>; READERS],
) {
    assert!(READERS > 0, "observation publication requires a reader");
    let faults = Arc::new(Faults {
        imu: std::array::from_fn(|_| AtomicBool::new(false)),
        gnss: AtomicBool::new(false),
    });
    let mut imu_readers: [[Option<_>; READERS]; IMUS] =
        std::array::from_fn(|_| std::array::from_fn(|_| None));
    let imu = std::array::from_fn(|index| {
        let (samples, readers) = latest::channel(None);
        imu_readers[index] = readers.map(Some);
        ImuPublisher {
            index,
            samples,
            faults: faults.clone(),
        }
    });
    let mut gnss_readers: [Option<GnssReader>; READERS] = std::array::from_fn(|_| None);
    let gnss = gnss_fresh_for.map(|fresh_for| {
        let (fixes, fixes_readers) = latest::channel(None);
        let (satellites, satellites_readers) = latest::channel(None);
        for ((slot, fixes), satellites) in gnss_readers
            .iter_mut()
            .zip(fixes_readers)
            .zip(satellites_readers)
        {
            *slot = Some(GnssReader {
                fixes,
                satellites,
                fresh_for,
            });
        }
        GnssPublisher {
            fixes,
            satellites,
            faults: faults.clone(),
        }
    });
    let readers = std::array::from_fn(|reader| ObservationReader {
        imu: std::array::from_fn(|source| imu_readers[source][reader].take().unwrap()),
        imu_fresh_for,
        gnss: gnss_readers[reader].take(),
        faults: faults.clone(),
    });
    (ObservationPublishers { imu, gnss }, readers)
}

enum Values<const IMUS: usize> {
    Local(Observations<IMUS>),
    Published(ObservationReader<IMUS>),
}

/// Service mode whose owner receives and records each observation.
pub enum Local {}

/// Service mode whose acquisition callbacks own the publication endpoints.
pub enum Published {}

/// One owner for the sessions and their readings. The caller supplies the
/// inbox, allowing sensor events to share its wait point with other inputs.
/// Session handles are allocated once on open to support heterogeneous devices.
pub struct ObservationService<const IMUS: usize, Mode = Local> {
    values: Values<IMUS>,
    imu: [Option<Box<dyn imu::Session + Send>>; IMUS],
    gnss: Option<Box<dyn gnss::Session + Send>>,
    stopping: bool,
    mode: PhantomData<Mode>,
}

impl<const IMUS: usize> ObservationService<IMUS> {
    pub fn new(imu_fresh_for: Duration, gnss_fresh_for: Option<Duration>) -> Self {
        Self::with_values(Values::Local(Observations::new(
            imu_fresh_for,
            gnss_fresh_for,
        )))
    }

    pub fn observe(&mut self, observation: Observation) -> Option<SensorFault> {
        let Values::Local(values) = &mut self.values else {
            unreachable!();
        };
        values.observe(&observation)
    }

    pub fn snapshot(&self, now: Instant) -> Snapshot<IMUS> {
        let Values::Local(values) = &self.values else {
            unreachable!();
        };
        values.snapshot(now)
    }
}

impl<const IMUS: usize> ObservationService<IMUS, Published> {
    /// Move one fixed reader to the acquisition owner. Ordinary observations
    /// are published only by the source endpoints; this mode deliberately has
    /// no `observe` method. Startup and reap retain terminal faults separately.
    ///
    /// ```compile_fail
    /// use navigation::observations::{publication, Observation, ObservationService};
    /// use std::time::{Duration, Instant};
    /// let (_, [reader]) = publication::<1, 1>(Duration::from_secs(1), None);
    /// let mut owner = ObservationService::with_reader(reader);
    /// owner.observe(Observation::imu(
    ///     0, imu::Event::Failed(imu::DeviceError::Io), Instant::now(),
    /// ));
    /// ```
    pub fn with_reader(reader: ObservationReader<IMUS>) -> Self {
        Self::with_values(Values::Published(reader))
    }

    pub fn snapshot(&mut self, now: Instant) -> Snapshot<IMUS> {
        let Values::Published(reader) = &mut self.values else {
            unreachable!();
        };
        reader.snapshot(now)
    }

    pub fn satellites(&mut self, now: Instant) -> Reading<gnss::SatelliteReport> {
        let Values::Published(reader) = &mut self.values else {
            unreachable!();
        };
        reader.satellites(now)
    }
}

impl<const IMUS: usize, Mode> ObservationService<IMUS, Mode> {
    fn with_values(values: Values<IMUS>) -> Self {
        let gnss_configured = match &values {
            Values::Local(values) => values.gnss.is_some(),
            Values::Published(reader) => reader.gnss.is_some(),
        };
        crate::diagnostics::configured(IMUS, gnss_configured);
        Self {
            values,
            imu: std::array::from_fn(|_| None),
            gnss: None,
            stopping: false,
            mode: PhantomData,
        }
    }

    fn imu_faulted(&self, index: usize) -> bool {
        match &self.values {
            Values::Local(values) => matches!(values.imu[index].latest, Latest::Fault),
            Values::Published(reader) => reader.faults.imu[index].load(Ordering::Acquire),
        }
    }

    fn imu_failed(&mut self, index: usize) -> bool {
        match &mut self.values {
            Values::Local(values) => values.imu_failed(index),
            Values::Published(reader) => reader.faults.imu_failed(index),
        }
    }

    fn gnss_faulted(&self) -> Option<bool> {
        match &self.values {
            Values::Local(values) => values
                .gnss
                .as_ref()
                .map(|source| matches!(source.latest, Latest::Fault)),
            Values::Published(reader) => reader
                .gnss
                .as_ref()
                .map(|_| reader.faults.gnss.load(Ordering::Acquire)),
        }
    }

    fn gnss_failed(&mut self) -> bool {
        match &mut self.values {
            Values::Local(values) => values.gnss_failed(),
            Values::Published(reader) => reader.gnss.is_some() && reader.faults.gnss_failed(),
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
        if self.stopping || slot.is_some() || self.imu_faulted(index) {
            return Err(SensorFault::Imu(index, imu::DeviceError::Busy));
        }
        match device.open_default(sink(sender, move |event| Input::Imu(index, event), wrap)) {
            Ok(session) => {
                self.imu[index] = Some(Box::new(session));
                Ok(())
            }
            Err(error) => {
                self.imu_failed(index);
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
        let Some(failed) = self.gnss_faulted() else {
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
                self.gnss_failed();
                Err(SensorFault::Gnss(error))
            }
        }
    }

    /// Reconcile terminal results before publishing a snapshot, including
    /// faults whose notification could not be admitted to the inbox.
    pub fn reap_finished(&mut self, mut report: impl FnMut(SensorFault)) {
        // Latest state is published by the sink. Report the joined result
        // once even if the display's wake notification was lost.
        let published = matches!(self.values, Values::Published(_));
        for index in 0..IMUS {
            let slot = &mut self.imu[index];
            if slot.as_ref().is_some_and(|session| session.is_finished()) {
                let error = slot
                    .take()
                    .unwrap()
                    .join()
                    .err()
                    .unwrap_or(imu::DeviceError::Io);
                if self.imu_failed(index) || published {
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
            if self.gnss_failed() || published {
                report(SensorFault::Gnss(error));
            }
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

    fn sample(sequence: u64) -> imu::ImuSample {
        imu::ImuSample {
            sequence,
            timestamp_us: sequence * 10_000,
            is_capture_time: false,
            sample_interval_us: 10_000,
            accel_mps2: [0.0, 0.0, 9.8],
            gyro_rps: [0.0; 3],
        }
    }

    #[test]
    fn readers_coalesce_independently_without_renewing_source_freshness() {
        let freshness = Duration::from_millis(500);
        let (publishers, [mut active, mut delayed]) = publication::<1, 2>(freshness, None);
        let [mut publisher] = publishers.imu;
        let received = Instant::now();
        for sequence in 1..=100 {
            publisher
                .record(&Observation::imu(
                    0,
                    imu::Event::Sample(sample(sequence)),
                    received,
                ))
                .unwrap();
            assert_eq!(
                active.snapshot(received).imu,
                [Reading::Live(sample(sequence))]
            );
        }
        assert_eq!(delayed.snapshot(received).imu, [Reading::Live(sample(100))]);
        drop(publisher);
        assert_eq!(active.snapshot(received).imu, [Reading::Live(sample(100))]);
        assert_eq!(delayed.snapshot(received + freshness).imu, [Reading::Stale]);
        assert_eq!(delayed.snapshot(received).gnss, None);
    }

    #[test]
    fn reaping_a_publisher_that_dropped_without_notification_faults_every_reader() {
        let (publishers, [owner, mut other]) =
            publication::<1, 2>(Duration::from_millis(500), None);
        let [mut publisher] = publishers.imu;
        let received = Instant::now();
        publisher
            .record(&Observation::imu(
                0,
                imu::Event::Sample(sample(1)),
                received,
            ))
            .unwrap();
        let mut service = ObservationService::with_reader(owner);
        service.imu[0] = Some(Box::new(FinishedImu));
        drop(publisher);
        let mut faults = Vec::new();
        service.reap_finished(|fault| faults.push(fault));
        service.reap_finished(|fault| faults.push(fault));
        assert_eq!(faults, [SensorFault::Imu(0, imu::DeviceError::Io)]);
        assert_eq!(service.snapshot(received).imu, [Reading::Fault]);
        assert_eq!(other.snapshot(received).imu, [Reading::Fault]);
    }

    #[test]
    #[should_panic(expected = "IMU publisher ordinal mismatch")]
    fn source_wiring_cannot_publish_another_imus_observation() {
        let (publishers, _) = publication::<2, 1>(Duration::from_secs(1), None);
        let [mut first, _] = publishers.imu;
        let _ = first.record(&Observation::imu(
            1,
            imu::Event::Sample(sample(1)),
            Instant::now(),
        ));
    }

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
    fn published_satellites_remain_live_across_a_slow_receiver_interval() {
        let (publishers, [owner, mut readings]) =
            publication::<0, 2>(Duration::ZERO, Some(Duration::from_secs(3)));
        let mut service = ObservationService::with_reader(owner);
        let mut receiver = publishers.gnss.unwrap();
        let now = Instant::now();
        let report = gnss::SatelliteReport::new(4, 0, None, 10_000, 0, []);
        assert!(receiver
            .record(&Observation::gnss(gnss::Event::Satellites(report), now))
            .unwrap()
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
        assert_eq!(service.satellites(now), Reading::Live(report));
        assert!(receiver
            .record(&Observation::gnss(
                gnss::Event::Failed(gnss::DeviceError::Io),
                now,
            ))
            .is_err());
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
    fn published_samples_are_visible_without_dispatching_the_owner_inbox() {
        let (publishers, [owner, mut readings]) =
            publication::<1, 2>(Duration::from_millis(500), None);
        let mut service = ObservationService::with_reader(owner);
        let [mut publisher] = publishers.imu;
        let (sender, inbox) = messaging::bounded(1);
        let pending = sender.clone();
        let mut deliver = sink(
            sender,
            |event| Input::Imu(0, event),
            move |event| publisher.record(&event).is_err().then_some(event),
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
