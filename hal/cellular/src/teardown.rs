//! Guarded SIM7080 escape under one parent cleanup deadline.
use crate::{Error, SHUTDOWN_TIMEOUT};
use std::time::{Duration, Instant};

pub(crate) const COMMAND: Duration = Duration::from_secs(2);
const GUARD: Duration = Duration::from_millis(1100);

// This private seam lets software deadline/quarantine policy run under a
// deterministic clock without claiming to qualify the physical UART/modem.
pub(crate) trait Control {
    fn now(&self) -> Instant;
    fn pause(&mut self, duration: Duration);
    fn write(&mut self, bytes: &[u8], deadline: Instant) -> Result<(), Error>;
    fn ok(&mut self, deadline: Instant) -> Result<(), Error>;
}

fn guard(control: &mut impl Control, deadline: Instant) -> Result<(), Error> {
    if deadline.saturating_duration_since(control.now()) < GUARD {
        return Err(Error::RecoveryRequired);
    }
    control.pause(GUARD);
    if control.now() >= deadline {
        return Err(Error::RecoveryRequired);
    }
    Ok(())
}

fn command(control: &mut impl Control, bytes: &[u8], deadline: Instant) -> Result<(), Error> {
    let deadline = deadline.min(control.now() + COMMAND);
    control.write(bytes, deadline)?;
    control.ok(deadline)
}

/// Cancellation has stopped PPP. The identity of the data/command mode is
/// still uncertain until ATH or AT is acknowledged; failure quarantines it.
pub(crate) fn command_mode(control: &mut impl Control) -> Result<(), Error> {
    let deadline = control.now() + SHUTDOWN_TIMEOUT;
    guard(control, deadline)?;
    control
        .write(b"+++", deadline.min(control.now() + COMMAND))
        .map_err(|_| Error::RecoveryRequired)?;
    guard(control, deadline)?;
    let _ = control.ok(deadline.min(control.now() + COMMAND));
    if command(control, b"ATH\r", deadline).is_err() && command(control, b"AT\r", deadline).is_err()
    {
        return Err(Error::RecoveryRequired);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    struct Modem {
        now: Instant,
        responses: VecDeque<(Duration, Result<(), Error>)>,
        write_delay: Duration,
        scheduler_delay: Duration,
        writes: Vec<Vec<u8>>,
        deadlines: Vec<Instant>,
    }

    impl Control for Modem {
        fn now(&self) -> Instant {
            self.now
        }
        fn pause(&mut self, duration: Duration) {
            self.now += duration + self.scheduler_delay;
        }
        fn write(&mut self, bytes: &[u8], deadline: Instant) -> Result<(), Error> {
            self.deadlines.push(deadline);
            if self.now >= deadline {
                return Err(Error::Timeout);
            }
            self.now = (self.now + self.write_delay).min(deadline);
            if self.now >= deadline {
                return Err(Error::Timeout);
            }
            self.writes.push(bytes.to_vec());
            Ok(())
        }
        fn ok(&mut self, deadline: Instant) -> Result<(), Error> {
            self.deadlines.push(deadline);
            if self.now >= deadline {
                return Err(Error::Timeout);
            }
            let (delay, result) = self.responses.pop_front().unwrap();
            self.now = (self.now + delay).min(deadline);
            if self.now >= deadline {
                Err(Error::Timeout)
            } else {
                result
            }
        }
    }

    fn modem(
        now: Instant,
        responses: impl IntoIterator<Item = (Duration, Result<(), Error>)>,
    ) -> Modem {
        Modem {
            now,
            responses: responses.into_iter().collect(),
            write_delay: Duration::ZERO,
            scheduler_delay: Duration::ZERO,
            writes: Vec::new(),
            deadlines: Vec::new(),
        }
    }

    #[test]
    fn missing_escape_reply_requires_a_command_mode_acknowledgement() {
        let start = Instant::now();
        let mut control = modem(
            start,
            [
                (Duration::from_secs(2), Err(Error::Timeout)),
                (Duration::ZERO, Err(Error::Rejected)),
                (Duration::ZERO, Ok(())),
            ],
        );
        assert_eq!(command_mode(&mut control), Ok(()));
        assert_eq!(
            control.writes,
            [b"+++".to_vec(), b"ATH\r".to_vec(), b"AT\r".to_vec()]
        );
        assert!(control.now <= start + SHUTDOWN_TIMEOUT);
    }

    #[test]
    fn silent_modem_is_quarantined_within_the_cumulative_budget() {
        let start = Instant::now();
        let mut control = modem(start, [(COMMAND, Err(Error::Timeout)); 3]);
        assert_eq!(command_mode(&mut control), Err(Error::RecoveryRequired));
        assert_eq!(control.now - start, Duration::from_millis(8_200));
        assert!(control
            .deadlines
            .iter()
            .all(|deadline| *deadline <= start + SHUTDOWN_TIMEOUT));
    }

    #[test]
    fn delayed_scheduling_cannot_refresh_child_budgets() {
        let start = Instant::now();
        let mut control = modem(start, [(COMMAND, Err(Error::Timeout)); 3]);
        control.scheduler_delay = Duration::from_secs(2);
        assert_eq!(command_mode(&mut control), Err(Error::RecoveryRequired));
        assert_eq!(control.now, start + SHUTDOWN_TIMEOUT);
        assert!(control
            .deadlines
            .iter()
            .all(|deadline| *deadline <= start + SHUTDOWN_TIMEOUT));
    }

    #[test]
    fn escape_write_failure_never_releases_uncertain_data_mode() {
        let start = Instant::now();
        let mut control = modem(start, []);
        control.write_delay = COMMAND;
        assert_eq!(command_mode(&mut control), Err(Error::RecoveryRequired));
        assert!(control.writes.is_empty());
    }
}
