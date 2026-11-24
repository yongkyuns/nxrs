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
        self.offset_ms
            .checked_add(i64::try_from(monotonic_ms).ok()?)
    }
}

pub(crate) fn observe(
    slot: &std::sync::OnceLock<ClockAnchor>,
    sample: navigation::observations::Measurement,
) {
    if let navigation::observations::Measurement::Gnss(fix) = sample {
        if let Some(anchor) = fix
            .receiver_utc_ms
            .and_then(|utc| ClockAnchor::new(fix.timestamp_ms, utc))
        {
            let _ = slot.set(anchor);
        }
    }
}

#[cfg(not(feature = "ble-link"))]
pub(crate) struct Capture(std::sync::OnceLock<ClockAnchor>);

#[cfg(not(feature = "ble-link"))]
impl Capture {
    pub fn new() -> Self {
        Self(std::sync::OnceLock::new())
    }

    pub fn clock(&self) -> Option<ClockAnchor> {
        self.0.get().copied()
    }

    pub fn record(&self, sample: navigation::observations::Measurement) {
        observe(&self.0, sample);
    }
}
