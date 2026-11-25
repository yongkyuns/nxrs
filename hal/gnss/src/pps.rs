//! Match the receiver's next-pulse report to a retained monotonic GPIO edge.
use peripheral::gnss::{Measurement, TimePulse};

const SECOND_US: u64 = 1_000_000;
const WEEK_MS: i64 = 604_800_000;
const GPS_EPOCH_UNIX_MS: i64 = 315_964_800_000;

#[derive(Default)]
pub(crate) struct Sync {
    edge: Option<(usize, u64)>,
    pending: Option<(TimePulse, u64)>,
    anchor: Option<TimePulse>,
    locked: bool,
}

impl Sync {
    pub(crate) fn report(&mut self, pulse: Option<TimePulse>, received_us: u64) {
        self.pending = pulse.map(|pulse| (pulse, received_us));
        if pulse.is_none() {
            self.anchor = None;
            self.locked = false;
        }
    }

    pub(crate) fn edge(&mut self, sequence: usize, time: u64) {
        let previous = self.edge.replace((sequence, time));
        let pulse = self.pending.take().and_then(|(pulse, received)| {
            (1..SECOND_US)
                .contains(&time.checked_sub(received)?)
                .then_some(pulse)
        });
        // Require two consecutive associations before trusting a fix. An
        // overwritten/missing GPIO edge or receiver reset drops the lock.
        self.locked = previous.is_some_and(|(count, prior)| {
            count.wrapping_add(1) == sequence
                && (900_000..=1_100_000).contains(&time.saturating_sub(prior))
        }) && matches!(
            (self.anchor, pulse),
            (Some(TimePulse::Gps(a)), Some(TimePulse::Gps(b)))
                | (Some(TimePulse::Utc(a)), Some(TimePulse::Utc(b)))
                if (i64::from(b) - i64::from(a)).rem_euclid(WEEK_MS) == 1_000
        );
        self.anchor = pulse;
    }

    pub(crate) fn fix_time(&self, report: &Measurement, received_us: u64) -> Option<u64> {
        let (_, edge_us) = self.edge?;
        if !self.locked || received_us.checked_sub(edge_us)? >= SECOND_US {
            return None;
        }
        let (epoch_ms, anchor_ms) = match self.anchor? {
            TimePulse::Gps(anchor) => (i64::try_from(report.epoch_ms).ok()?, anchor),
            TimePulse::Utc(anchor) => (
                report
                    .receiver_utc_ms?
                    .checked_sub(GPS_EPOCH_UNIX_MS)?
                    .rem_euclid(WEEK_MS),
                anchor,
            ),
        };
        if !(0..WEEK_MS).contains(&epoch_ms) {
            return None;
        }
        let delta_ms =
            (epoch_ms - i64::from(anchor_ms) + WEEK_MS / 2).rem_euclid(WEEK_MS) - WEEK_MS / 2;
        let capture = edge_us.checked_add_signed(delta_ms * 1_000)?;
        // Reject delayed/ambiguous associations, including an off-by-one
        // pulse pairing. At 38400 baud the nominal NAV-PVT wire time is 26 ms.
        (received_us.checked_sub(capture)? < 400_000).then_some(capture)
    }
}
