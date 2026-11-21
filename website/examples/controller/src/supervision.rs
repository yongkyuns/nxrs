//! Optional watchdog policy for this application; no additional worker.
use crate::State;
use std::time::{Duration, Instant};
use watchdog::{DeviceError, RuntimeWatchdog};

pub(crate) const CHECK_INTERVAL: Duration = Duration::from_millis(100);

pub(crate) struct Supervisor<W> {
    device: Option<W>,
    last_feed: Instant,
    fault: Option<DeviceError>,
}

impl<W: RuntimeWatchdog> Supervisor<W> {
    pub fn new(device: Option<W>) -> Self {
        Self {
            device,
            last_feed: Instant::now(),
            fault: None,
        }
    }
    pub fn enabled(&self) -> bool {
        self.device.is_some()
    }
    pub fn fault(&self) -> Option<DeviceError> {
        self.fault
    }
    pub fn check(&mut self, now: Instant, healthy: bool) {
        if !healthy
            || self.fault.is_some()
            || now.saturating_duration_since(self.last_feed) < CHECK_INTERVAL
        {
            return;
        }
        if let Some(device) = &mut self.device {
            match device.feed() {
                Ok(()) => self.last_feed = now,
                Err(error) => self.fault = Some(error),
            }
        }
    }
    pub fn finish(&mut self, cleaned_up: bool) -> Result<(), DeviceError> {
        // A terminal source fault or uncertain shutdown leaves supervision
        // armed, even if the source was successfully retired. Only a clean
        // explicit stop disarms after acquisition has joined.
        if let Some(device) = &mut self.device {
            if !cleaned_up {
                return Err(*self.fault.get_or_insert(DeviceError::InvalidState));
            }
            if let Err(error) = device.disarm() {
                self.fault.get_or_insert(error);
            }
        }
        self.fault.map_or(Ok(()), Err)
    }
}

pub(crate) fn healthy(
    state: State,
    configured: Instant,
    received: Option<Instant>,
    now: Instant,
) -> bool {
    match state {
        State::Off => true,
        State::Unknown(_) => false,
        State::Running(wave) => {
            let progress = received.map_or(configured, |received| received.max(configured));
            let grace =
                Duration::from_nanos(2_000_000_000u64.div_ceil(u64::from(wave.frequency_hz())))
                    + Duration::from_millis(250);
            now.saturating_duration_since(progress) <= grace
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pulse::{Error, Waveform};
    use std::sync::{Arc, Mutex};
    use watchdog::ArmedWatchdog;

    struct Probe(Arc<Mutex<Vec<&'static str>>>);
    impl ArmedWatchdog for Probe {
        fn disarm(&mut self) -> Result<(), DeviceError> {
            self.0.lock().unwrap().push("disarm");
            Ok(())
        }
    }
    impl RuntimeWatchdog for Probe {
        fn feed(&mut self) -> Result<(), DeviceError> {
            self.0.lock().unwrap().push("feed");
            Ok(())
        }
    }

    #[test]
    fn feed_requires_owner_and_source_progress_and_cleanup_precedes_disarm() {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let mut supervisor = Supervisor::new(Some(Probe(calls.clone())));
        let start = supervisor.last_feed;
        let running = State::Running(Waveform::new(25, 25).unwrap());
        assert!(healthy(
            running,
            start,
            None,
            start + Duration::from_millis(100)
        ));
        assert!(!healthy(
            running,
            start,
            None,
            start + Duration::from_secs(1)
        ));
        assert!(healthy(
            running,
            start,
            Some(start + Duration::from_secs(1)),
            start + Duration::from_secs(1)
        ));
        assert!(!healthy(
            State::Unknown(Error::Io),
            start,
            Some(start),
            start
        ));
        supervisor.check(start + CHECK_INTERVAL, true);
        supervisor.check(start + CHECK_INTERVAL * 2, false);
        assert!(supervisor.finish(false).is_err());
        assert_eq!(*calls.lock().unwrap(), ["feed"]);
        assert_eq!(supervisor.fault(), Some(DeviceError::InvalidState));
        assert_eq!(supervisor.finish(true), Err(DeviceError::InvalidState));
        assert_eq!(*calls.lock().unwrap(), ["feed", "disarm"]);
    }

    #[test]
    fn explicit_clean_stop_disarms_and_device_failures_remain_visible() {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let mut supervisor = Supervisor::new(Some(Probe(calls.clone())));
        supervisor.finish(true).unwrap();
        assert_eq!(*calls.lock().unwrap(), ["disarm"]);
        assert_eq!(supervisor.fault(), None);

        struct Failing;
        impl ArmedWatchdog for Failing {
            fn disarm(&mut self) -> Result<(), DeviceError> {
                Err(DeviceError::Io)
            }
        }
        impl RuntimeWatchdog for Failing {
            fn feed(&mut self) -> Result<(), DeviceError> {
                Err(DeviceError::Io)
            }
        }
        let mut supervisor = Supervisor::new(Some(Failing));
        supervisor.check(supervisor.last_feed + CHECK_INTERVAL, true);
        assert_eq!(supervisor.fault(), Some(DeviceError::Io));
        assert_eq!(supervisor.finish(true), Err(DeviceError::Io));
        let mut supervisor = Supervisor::new(Some(Failing));
        assert_eq!(supervisor.finish(true), Err(DeviceError::Io));
        assert_eq!(supervisor.fault(), Some(DeviceError::Io));
    }
}
