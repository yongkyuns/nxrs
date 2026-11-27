//! Product supervision. The hardware timer bounds supervisor stalls; the
//! shutdown deadline bounds cleanup even while the supervisor keeps progressing.
use std::{
    io,
    sync::atomic::{AtomicU8, Ordering},
    time::{Duration, Instant},
};
use watchdog::{DeviceError, RuntimeWatchdog};

const TIMEOUT: Duration = Duration::from_secs(8);
// Cellular cleanup consumes its declared 10.2 s software budget. Concurrent
// cancellation leaves 1.8 s for retirement/scheduling. This product deadline
// is not a claim about qualified native worst-case execution time.
pub(super) const SHUTDOWN_BUDGET: Duration = Duration::from_secs(12);

#[derive(Clone, Copy)]
pub(super) enum Component {
    Display = 1,
    Touch = 2,
    Usb = 4,
    Communications = 8,
}

/// Sticky optional-capability failures, independent of notification admission.
#[derive(Default)]
pub(super) struct Status(AtomicU8);

impl Status {
    pub fn fail(&self, component: Component) {
        self.0.fetch_or(component as u8, Ordering::Release);
    }

    pub fn snapshot(&self) -> u8 {
        self.0.load(Ordering::Acquire)
    }
}

pub(super) struct Supervisor<'a> {
    timer: Option<&'a mut dyn RuntimeWatchdog>,
    quiescent: bool,
    shutdown_deadline: Option<Instant>,
}

impl Supervisor<'_> {
    /// Call before admitting an independently progressing owner.
    pub fn owners_started(&mut self) {
        self.quiescent = false;
    }

    pub fn begin_shutdown(&mut self, now: Instant) -> Instant {
        *self.shutdown_deadline.get_or_insert(now + SHUTDOWN_BUDGET)
    }

    pub fn owners_retired(&mut self) {
        self.quiescent = true;
    }

    /// Known contained faults permit degraded operation. A feed proves the
    /// supervisor serviced retained state, not that every sensor has a fix.
    pub fn progress(&mut self, now: Instant) -> io::Result<()> {
        if self
            .shutdown_deadline
            .is_some_and(|deadline| now >= deadline)
        {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "tracker shutdown deadline",
            ));
        }
        if let Some(timer) = &mut self.timer {
            timer
                .feed()
                .map_err(|error| io::Error::other(format!("feed tracker watchdog: {error:?}")))?;
        }
        Ok(())
    }
}

pub(super) fn run(work: impl FnOnce(&mut Supervisor<'_>) -> io::Result<()>) -> io::Result<()> {
    if let Ok(cause) = boot::reset_cause() {
        log::info!("motion-tracker reset cause: {cause:?}");
    }
    let mut timer = match watchdog::arm(TIMEOUT) {
        Ok(timer) => Some(timer),
        Err(DeviceError::Unsupported) => None,
        Err(error) => return Err(io::Error::other(format!("arm tracker watchdog: {error:?}"))),
    };
    supervise(
        timer
            .as_mut()
            .map(|timer| timer as &mut dyn RuntimeWatchdog),
        work,
    )
}

fn supervise(
    timer: Option<&mut dyn RuntimeWatchdog>,
    work: impl FnOnce(&mut Supervisor<'_>) -> io::Result<()>,
) -> io::Result<()> {
    let mut supervisor = Supervisor {
        timer,
        quiescent: true,
        shutdown_deadline: None,
    };
    let result = work(&mut supervisor);
    if !supervisor.quiescent {
        // Dropping the handle leaves the hardware timer armed. Failed
        // cancellation must never masquerade as completed shutdown.
        log::error!("motion-tracker owners did not retire; watchdog remains armed");
        return result.and(Err(io::Error::other("tracker shutdown incomplete")));
    }
    if let Some(timer) = &mut supervisor.timer {
        if let Err(error) = timer.disarm() {
            log::error!("motion-tracker disarm watchdog: {error:?}");
            return result.and(Err(io::Error::other("disarm tracker watchdog")));
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use watchdog::ArmedWatchdog;

    #[derive(Default)]
    struct Timer {
        feeds: usize,
        disarmed: bool,
    }
    impl ArmedWatchdog for Timer {
        fn disarm(&mut self) -> Result<(), DeviceError> {
            self.disarmed = true;
            Ok(())
        }
    }
    impl RuntimeWatchdog for Timer {
        fn feed(&mut self) -> Result<(), DeviceError> {
            self.feeds += 1;
            Ok(())
        }
    }

    #[test]
    fn failed_retirement_does_not_disarm_even_after_an_error() {
        let mut timer = Timer::default();
        let result = supervise(Some(&mut timer), |supervisor| {
            supervisor.owners_started();
            Err(io::Error::other("cancel wake failed"))
        });
        assert_eq!(result.unwrap_err().to_string(), "cancel wake failed");
        assert!(!timer.disarmed);
    }

    #[test]
    fn quiescent_failure_can_disarm_but_success_cannot_hide_pending_owners() {
        let mut timer = Timer::default();
        assert!(supervise(Some(&mut timer), |supervisor| {
            supervisor.owners_started();
            supervisor.owners_retired();
            Err(io::Error::other("contained source fault"))
        })
        .is_err());
        assert!(timer.disarmed);
        timer.disarmed = false;
        assert!(supervise(Some(&mut timer), |supervisor| {
            supervisor.owners_started();
            Ok(())
        })
        .is_err());
        assert!(!timer.disarmed);
    }

    #[test]
    fn shutdown_feeds_are_bounded_by_one_parent_deadline() {
        assert!(SHUTDOWN_BUDGET > cellular::SHUTDOWN_TIMEOUT);
        let mut timer = Timer::default();
        let start = Instant::now();
        assert!(supervise(Some(&mut timer), |supervisor| {
            supervisor.owners_started();
            let deadline = supervisor.begin_shutdown(start);
            assert_eq!(
                supervisor.begin_shutdown(start + Duration::from_secs(2)),
                deadline
            );
            supervisor.progress(start + Duration::from_secs(9))?;
            supervisor.progress(deadline)
        })
        .is_err());
        assert_eq!(timer.feeds, 1);
        assert!(!timer.disarmed);
    }

    #[test]
    fn optional_faults_accumulate_without_a_notification_queue() {
        let status = Status::default();
        status.fail(Component::Display);
        status.fail(Component::Usb);
        status.fail(Component::Display);
        assert_eq!(status.snapshot(), 5);
    }
}
