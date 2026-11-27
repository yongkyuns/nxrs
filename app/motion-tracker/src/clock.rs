//! One UTC mapping for tracker telemetry. The GNSS solution anchors the host
//! clock at reception time, so UART latency limits alignment to the solution.
#[derive(Clone, Copy)]
pub(crate) struct ClockAnchor {
    pub(crate) since_ms: u64,
    pub(crate) offset_ms: i64,
}

impl ClockAnchor {
    pub(crate) fn new(monotonic_ms: u64, utc_ms: i64) -> Option<Self> {
        Some(Self {
            since_ms: monotonic_ms,
            offset_ms: utc_ms.checked_sub(i64::try_from(monotonic_ms).ok()?)?,
        })
    }

    pub(crate) fn utc_ms(self, monotonic_ms: u64) -> Option<i64> {
        if monotonic_ms < self.since_ms {
            return None;
        }
        self.offset_ms.checked_add(i64::try_from(monotonic_ms).ok()?)
    }
}
