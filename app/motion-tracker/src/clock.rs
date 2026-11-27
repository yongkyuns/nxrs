//! Bounded UTC mapping from capture-qualified GNSS evidence. Monotonic sample
//! times remain authoritative. No oscillator drift bound is assumed, so mapping
//! validity ends at the GNSS freshness budget instead of entering silent holdover.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct ClockAnchor {
    pub(crate) since_ms: u64,
    pub(crate) until_ms: u64,
    pub(crate) offset_ms: i64,
    pub(crate) generation: u32,
}

impl ClockAnchor {
    pub(crate) fn new(monotonic_ms: u64, utc_ms: i64) -> Option<Self> {
        Some(Self {
            since_ms: monotonic_ms,
            until_ms: monotonic_ms.checked_add(crate::tracker::FIX_FRESH.as_millis() as u64)?,
            offset_ms: utc_ms.checked_sub(i64::try_from(monotonic_ms).ok()?)?,
            generation: 0,
        })
    }

    pub(crate) fn from_fix(fix: gnss::GnssFix) -> Option<Self> {
        if !fix.is_capture_time
            || fix.received_us.checked_sub(fix.timestamp_us)?
                >= crate::tracker::FIX_FRESH.as_micros() as u64
        {
            return None;
        }
        let mut anchor = Self::new(fix.timestamp_us / 1_000, fix.receiver_utc_ms?)?;
        anchor.generation = fix.generation;
        Some(anchor)
    }

    pub(crate) fn utc_ms(self, monotonic_ms: u64) -> Option<i64> {
        if monotonic_ms < self.since_ms || monotonic_ms >= self.until_ms {
            return None;
        }
        self.offset_ms
            .checked_add(i64::try_from(monotonic_ms).ok()?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mapping_has_a_half_open_evidence_window_and_no_unbounded_holdover() {
        let anchor = ClockAnchor::new(1_000, 1_700_000_000_000).unwrap();
        assert_eq!(anchor.utc_ms(999), None);
        assert_eq!(anchor.utc_ms(1_000), Some(1_700_000_000_000));
        assert_eq!(anchor.utc_ms(3_999), Some(1_700_000_002_999));
        assert_eq!(anchor.utc_ms(4_000), None);
        assert_eq!(anchor.utc_ms(86_401_000), None);
    }

    #[test]
    fn only_fresh_capture_qualified_fixes_establish_a_mapping() {
        let mut fix = gnss::GnssFix {
            generation: 9,
            sequence: 1,
            source_epoch_ms: 10,
            timestamp_us: 1_000_000,
            received_us: 1_000_000,
            is_capture_time: false,
            receiver_utc_ms: Some(100_000),
            latitude_deg: 45.0,
            longitude_deg: -76.0,
            speed_mps: 0.0,
            course_rad: None,
            quality: None,
        };
        assert!(ClockAnchor::from_fix(fix).is_none());
        fix.is_capture_time = true;
        assert_eq!(ClockAnchor::from_fix(fix).unwrap().generation, 9);
        fix.received_us = fix.timestamp_us - 1;
        assert!(ClockAnchor::from_fix(fix).is_none());
        fix.received_us = fix.timestamp_us + 3_000_000;
        assert!(ClockAnchor::from_fix(fix).is_none());
        assert!(ClockAnchor::new(u64::MAX, 100_000).is_none());
    }
}
