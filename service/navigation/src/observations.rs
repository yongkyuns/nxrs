//! Own configured acquisition sessions and track normalized observations.
//! HALs acquire and deliver samples. The caller dispatches events and deadlines.

use messaging::{DeliveryResult, Sender};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

static NEXT_SESSION: AtomicU32 = AtomicU32::new(0);

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

/// One observation per configured source. An absent source has no reading.
#[derive(Clone, Copy)]
pub struct Snapshot<const IMUS: usize> {
    pub imu: [Option<Reading<imu::ImuSample>>; IMUS],
    pub gnss: Option<Reading<gnss::GnssFix>>,
}

enum Input {
    Imu(usize, imu::Event),
    Gnss(gnss::Event),
}

pub struct Observation {
    input: Input,
    received: Instant,
    session: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SensorFault {
    Imu(usize, imu::DeviceError),
    Gnss(gnss::DeviceError),
}

impl std::fmt::Display for SensorFault {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Imu(index, error) => write!(f, "IMU {}: {error:?}", index.saturating_add(1)),
            Self::Gnss(error) => write!(f, "GNSS: {error:?}"),
        }
    }
}

impl std::error::Error for SensorFault {}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Requirement {
    Required,
    /// Unsupported devices are absent. Other failures remain visible.
    Optional,
}

/// Receives the caller's device selections during one startup transaction.
pub struct Inputs<'a, const IMUS: usize, E> {
    service: &'a mut ObservationService<IMUS>,
    imu_fresh_for: Duration,
    gnss_fresh_for: Option<Duration>,
    sender: Sender<E>,
    wrap: fn(Observation) -> E,
}

impl<const IMUS: usize, E: Send + 'static> Inputs<'_, IMUS, E> {
    pub fn imu(
        &mut self,
        index: usize,
        device: &'static impl imu::Device,
        requirement: Requirement,
    ) -> Result<(), SensorFault> {
        let Some(value) = self.service.imu_values.get_mut(index) else {
            return Err(SensorFault::Imu(index, imu::DeviceError::InvalidData));
        };
        if value.is_some() {
            return Err(SensorFault::Imu(index, imu::DeviceError::Busy));
        }
        let receive = sink(
            self.sender.clone(),
            move |event| Input::Imu(index, event),
            self.wrap,
            self.service.session,
        );
        match device.open_default(receive) {
            Ok(session) => {
                *value = Some(Source::new(self.imu_fresh_for));
                self.service.imu[index] = Some(Box::new(session));
            }
            Err(imu::DeviceError::Unsupported) if requirement == Requirement::Optional => {}
            Err(error) => {
                let fault = SensorFault::Imu(index, error);
                if requirement == Requirement::Required {
                    return Err(fault);
                }
                *value = Some(Source::new(self.imu_fresh_for));
                report_fault(value, fault);
            }
        }
        Ok(())
    }

    pub fn gnss(
        &mut self,
        device: &'static impl gnss::Device,
        requirement: Requirement,
    ) -> Result<(), SensorFault> {
        let fresh_for = self
            .gnss_fresh_for
            .ok_or(SensorFault::Gnss(gnss::DeviceError::InvalidData))?;
        let value = &mut self.service.gnss_value;
        if value.is_some() {
            return Err(SensorFault::Gnss(gnss::DeviceError::Busy));
        }
        let receive = sink(
            self.sender.clone(),
            Input::Gnss,
            self.wrap,
            self.service.session,
        );
        match device.open(receive) {
            Ok(session) => {
                *value = Some(Source::new(fresh_for));
                self.service.gnss = Some(Box::new(session));
            }
            Err(gnss::DeviceError::Unsupported) if requirement == Requirement::Optional => {}
            Err(error) => {
                let fault = SensorFault::Gnss(error);
                if requirement == Requirement::Required {
                    return Err(fault);
                }
                *value = Some(Source::new(fresh_for));
                report_fault(value, fault);
            }
        }
        Ok(())
    }
}

/// Owns acquisition sessions and typed readings. The caller supplies one inbox
/// for sensor events and any other inputs it needs to dispatch.
pub struct ObservationService<const IMUS: usize> {
    session: u32,
    imu_values: [Option<Source<imu::ImuSample>>; IMUS],
    gnss_value: Option<Source<gnss::GnssFix>>,
    imu: [Option<Box<dyn imu::Session + Send>>; IMUS],
    gnss: Option<Box<dyn gnss::Session + Send>>,
    imu_drops: [u32; IMUS],
    gnss_drops: u32,
    stopping: bool,
}

impl<const IMUS: usize> ObservationService<IMUS> {
    fn new() -> Self {
        Self {
            session: NEXT_SESSION.fetch_add(1, Ordering::Relaxed),
            imu_values: [None; IMUS],
            gnss_value: None,
            imu: std::array::from_fn(|_| None),
            gnss: None,
            imu_drops: [0; IMUS],
            gnss_drops: 0,
            stopping: false,
        }
    }

    /// Open the selected sources using the caller's inbox. If configuration or
    /// required startup fails, cancel and join all sessions already opened.
    pub fn start<E: Send + 'static>(
        imu_fresh_for: Duration,
        gnss_fresh_for: Option<Duration>,
        sender: Sender<E>,
        wrap: fn(Observation) -> E,
        configure: impl FnOnce(&mut Inputs<'_, IMUS, E>) -> Result<(), SensorFault>,
    ) -> Result<Self, SensorFault> {
        let mut service = Self::new();
        if let Err(error) = configure(&mut Inputs {
            service: &mut service,
            imu_fresh_for,
            gnss_fresh_for,
            sender,
            wrap,
        }) {
            let _ = service.join();
            return Err(error);
        }
        crate::diagnostics::configured(IMUS, gnss_fresh_for.is_some());
        Ok(service)
    }

    pub fn observe(&mut self, observation: Observation) {
        // The shared inbox may still contain events from a rolled-back start.
        if observation.session != self.session {
            return;
        }
        let received = observation.received;
        match observation.input {
            Input::Imu(index, imu::Event::Sample(sample)) => {
                if let Some(source) = &mut self.imu_values[index] {
                    source.receive(sample, received);
                }
            }
            Input::Gnss(gnss::Event::Fix(fix)) => {
                if let Some(source) = &mut self.gnss_value {
                    source.receive(fix, received);
                }
            }
            Input::Gnss(gnss::Event::InvalidFrame) => {}
            Input::Imu(index, imu::Event::Failed(error)) => {
                report_fault(&mut self.imu_values[index], SensorFault::Imu(index, error));
            }
            Input::Gnss(gnss::Event::Failed(error)) => {
                report_fault(&mut self.gnss_value, SensorFault::Gnss(error));
            }
        }
    }

    /// Reconcile completed sessions before reading their state. Terminal faults
    /// remain visible even if the failure event could not enter the inbox.
    pub fn snapshot(&mut self, now: Instant) -> Snapshot<IMUS> {
        let log_drops = log::log_enabled!(log::Level::Warn);
        for (index, slot) in self.imu.iter_mut().enumerate() {
            if let Some(session) = slot {
                if log_drops {
                    let count = session.dropped();
                    if count > self.imu_drops[index] {
                        log::warn!("IMU {} queue drops: {count}", index + 1);
                        self.imu_drops[index] = count;
                    }
                }
                if session.is_finished() {
                    let error = slot
                        .take()
                        .unwrap()
                        .join()
                        .err()
                        .unwrap_or(imu::DeviceError::Io);
                    report_fault(&mut self.imu_values[index], SensorFault::Imu(index, error));
                }
            }
        }
        if let Some(session) = &mut self.gnss {
            if log_drops {
                let count = session.dropped();
                if count > self.gnss_drops {
                    log::warn!("GNSS queue drops: {count}");
                    self.gnss_drops = count;
                }
            }
            if session.is_finished() {
                let error = self
                    .gnss
                    .take()
                    .unwrap()
                    .join()
                    .err()
                    .unwrap_or(gnss::DeviceError::Io);
                report_fault(&mut self.gnss_value, SensorFault::Gnss(error));
            }
        }
        Snapshot {
            imu: self
                .imu_values
                .each_ref()
                .map(|source| source.as_ref().map(|source| source.reading(now))),
            gnss: self.gnss_value.as_ref().map(|source| source.reading(now)),
        }
    }

    /// Signal every session before the caller joins any worker. Attempt all
    /// cancellations even on failure and return the first error.
    pub fn request_stop(&mut self) -> Result<(), SensorFault> {
        if self.stopping {
            return Ok(());
        }
        self.stopping = true;
        let mut first = None;
        for (index, slot) in self.imu.iter_mut().enumerate() {
            if let Some(error) = slot
                .as_mut()
                .and_then(|session| session.request_stop().err())
            {
                record_cleanup(&mut first, SensorFault::Imu(index, error));
            }
        }
        if let Some(error) = self
            .gnss
            .as_mut()
            .and_then(|session| session.request_stop().err())
        {
            record_cleanup(&mut first, SensorFault::Gnss(error));
        }
        first.map_or(Ok(()), Err)
    }

    /// Join all sessions, requesting cancellation first if needed.
    pub fn join(mut self) -> Result<(), SensorFault> {
        let mut first = self.request_stop().err();
        for (index, slot) in self.imu.iter_mut().enumerate() {
            if let Some(error) = slot.take().and_then(|mut session| session.join().err()) {
                record_cleanup(&mut first, SensorFault::Imu(index, error));
            }
        }
        if let Some(error) = self
            .gnss
            .take()
            .and_then(|mut session| session.join().err())
        {
            record_cleanup(&mut first, SensorFault::Gnss(error));
        }
        first.map_or(Ok(()), Err)
    }
}

fn report_fault<T: Copy>(source: &mut Option<Source<T>>, fault: SensorFault) {
    if source.as_mut().is_some_and(Source::fail) {
        log::warn!("{fault}");
    }
}

fn record_cleanup(first: &mut Option<SensorFault>, fault: SensorFault) {
    log::warn!("acquisition cleanup: {fault}");
    first.get_or_insert(fault);
}

fn sink<T: Copy, E>(
    sender: Sender<E>,
    input: impl Fn(T) -> Input,
    wrap: fn(Observation) -> E,
    session: u32,
) -> impl FnMut(T) -> DeliveryResult<T> {
    crate::inbox_sink(sender, move |value| {
        wrap(Observation {
            input: input(value),
            received: Instant::now(),
            session,
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use messaging::DeliveryError;
    use std::sync::atomic::AtomicU8;

    struct ReceiverSession(&'static AtomicU8);

    impl gnss::Session for ReceiverSession {
        fn request_stop(&mut self) -> Result<(), gnss::DeviceError> {
            assert_eq!(self.0.swap(2, Ordering::Relaxed), 1);
            Ok(())
        }

        fn join(&mut self) -> Result<(), gnss::DeviceError> {
            assert_eq!(self.0.swap(0, Ordering::Relaxed), 2);
            Ok(())
        }

        fn is_finished(&self) -> bool {
            false
        }

        fn dropped(&self) -> u32 {
            0
        }
    }

    #[test]
    fn startup_failure_joins_open_sessions_and_discards_their_queued_events_on_retry() {
        static STATE: AtomicU8 = AtomicU8::new(0);
        fn open(inputs: &mut Inputs<'_, 0, Observation>) -> Result<(), SensorFault> {
            assert_eq!(STATE.swap(1, Ordering::Relaxed), 0);
            inputs.service.gnss = Some(Box::new(ReceiverSession(&STATE)));
            inputs.service.gnss_value = inputs.gnss_fresh_for.map(Source::new);
            inputs
                .sender
                .try_send(Observation {
                    input: Input::Gnss(gnss::Event::Failed(gnss::DeviceError::Io)),
                    received: Instant::now(),
                    session: inputs.service.session,
                })
                .ok()
                .unwrap();
            Ok(())
        }

        let (sender, inbox) = messaging::bounded(2);
        let freshness = Duration::from_secs(1);
        let failure = SensorFault::Imu(0, imu::DeviceError::Unsupported);
        let failed = ObservationService::<0>::start(
            freshness,
            Some(freshness),
            sender.clone(),
            std::convert::identity,
            |inputs| {
                open(inputs)?;
                Err(failure)
            },
        );
        assert_eq!(failed.err(), Some(failure));
        let mut retry = ObservationService::<0>::start(
            freshness,
            Some(freshness),
            sender,
            std::convert::identity,
            open,
        )
        .unwrap();
        retry.observe(inbox.try_recv().unwrap());
        assert_eq!(retry.snapshot(Instant::now()).gnss, Some(Reading::Waiting));
        retry.observe(inbox.try_recv().unwrap());
        assert_eq!(retry.snapshot(Instant::now()).gnss, Some(Reading::Fault));
        retry.join().unwrap();
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
        let mut service = ObservationService::<1>::new();
        service.imu_values[0] = Some(Source::new(Duration::from_millis(500)));
        service.imu[0] = Some(Box::new(FinishedImu));
        let (sender, inbox) = messaging::bounded(1);
        let mut deliver = sink(
            sender,
            |event| Input::Imu(0, event),
            |event| event,
            service.session,
        );
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

        assert_eq!(service.snapshot(Instant::now()).imu, [Some(Reading::Fault)]);
        assert!(service.imu[0].is_none());
        service.observe(inbox.try_recv().unwrap());
        assert_eq!(service.snapshot(Instant::now()).imu, [Some(Reading::Fault)]);
    }

    #[test]
    fn snapshot_distinguishes_absent_waiting_live_and_stale_sources() {
        let freshness = Duration::from_millis(500);
        let time = Instant::now();
        let mut service = ObservationService::<2>::new();
        service.imu_values[0] = Some(Source::new(freshness));
        assert_eq!(service.snapshot(time).gnss, None);
        assert_eq!(service.snapshot(time).imu, [Some(Reading::Waiting), None]);

        let sample = imu::ImuSample {
            sequence: 4,
            timestamp_ms: 0,
            sample_interval_us: 10_000,
            accel_mps2: [0.0, 0.0, 9.8],
            gyro_rps: [0.0; 3],
        };
        service.observe(Observation {
            input: Input::Imu(0, imu::Event::Sample(sample)),
            received: time,
            session: service.session,
        });
        assert_eq!(
            service.snapshot(time).imu,
            [Some(Reading::Live(sample)), None]
        );
        assert_eq!(
            service.snapshot(time + freshness).imu,
            [Some(Reading::Stale), None]
        );
    }
}
