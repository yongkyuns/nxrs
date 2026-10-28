//! Named AP-only experiment. Immutable identity; no callback allocation or wait.
use std::sync::{
    atomic::{AtomicU32, AtomicU8, Ordering},
    OnceLock,
};
const BYPASS: u8 = 0;
const CLOSED: u8 = 1;
const READY: u8 = 2;
const FAULT: u8 = 3;
pub struct Gate {
    state: AtomicU8,
    source: OnceLock<[u8; 6]>,
    admitted: AtomicU32,
    rejected: AtomicU32,
    startup: AtomicU32,
}
impl Gate {
    pub const fn new() -> Self {
        Self {
            state: AtomicU8::new(BYPASS),
            source: OnceLock::new(),
            admitted: AtomicU32::new(0),
            rejected: AtomicU32::new(0),
            startup: AtomicU32::new(0),
        }
    }
    pub fn configure(&self, source: [u8; 6]) -> bool {
        if self
            .state
            .compare_exchange(BYPASS, CLOSED, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            self.state.store(FAULT, Ordering::Release);
            return false;
        }
        if source == [0; 6] || source[0] & 1 != 0 || self.source.set(source).is_err() {
            self.state.store(FAULT, Ordering::Release);
            return false;
        }
        true
    }
    pub fn open(&self) -> bool {
        self.source.get().is_some()
            && self
                .state
                .compare_exchange(CLOSED, READY, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
    }
    fn count(&self, value: &AtomicU32) -> bool {
        // The SDK RX owner writes; the control task only reads. Unexpected
        // contention or overflow is terminal rather than retried in a callback.
        let old = value.load(Ordering::Acquire);
        let Some(next) = old.checked_add(1) else {
            self.state.store(FAULT, Ordering::Release);
            return false;
        };
        if value
            .compare_exchange(old, next, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            self.state.store(FAULT, Ordering::Release);
            return false;
        }
        true
    }
    pub fn admit(&self, source: Option<&[u8; 6]>) -> bool {
        match self.state.load(Ordering::Acquire) {
            BYPASS => true,
            CLOSED => {
                self.count(&self.startup);
                false
            }
            READY => match source {
                Some(mac) if self.source.get() == Some(mac) => self.count(&self.admitted),
                Some(_) => {
                    self.count(&self.rejected);
                    false
                }
                None => {
                    self.state.store(FAULT, Ordering::Release);
                    false
                }
            },
            _ => false,
        }
    }
    pub fn faulted(&self) -> bool {
        self.state.load(Ordering::Acquire) == FAULT
    }
    pub fn snapshot(&self) -> [u32; 4] {
        [
            self.state.load(Ordering::Acquire) as u32,
            self.admitted.load(Ordering::Acquire),
            self.rejected.load(Ordering::Acquire),
            self.startup.load(Ordering::Acquire),
        ]
    }
}
pub static GATE: Gate = Gate::new();
#[cfg(target_os = "espidf")]
#[no_mangle]
pub unsafe extern "C" fn presence_source_admit(source: *const u8) -> bool {
    GATE.admit(source.cast::<[u8; 6]>().as_ref())
}
#[cfg(test)]
mod tests {
    use super::*;
    const AP: [u8; 6] = [2, 3, 4, 5, 6, 7];
    #[test]
    fn legacy_bypasses_and_named_case_waits_for_ready_then_matches_exact_source() {
        let g = Gate::new();
        assert!(g.admit(None));
        assert!(!g.open());
        assert!(g.configure(AP));
        assert!(!g.admit(Some(&AP)));
        assert!(g.open());
        assert!(g.admit(Some(&AP)));
        let mut other = AP;
        other[5] ^= 1;
        assert!(!g.admit(Some(&other)));
        assert_eq!(g.snapshot(), [2, 1, 1, 1]);
        assert!(!g.configure(other));
        assert!(g.faulted());
        assert!(!g.admit(Some(&AP)));
    }
    #[test]
    fn invalid_identity_missing_metadata_and_counter_overflow_fail_closed() {
        for mac in [[0; 6], [255; 6], [1, 2, 3, 4, 5, 6]] {
            let g = Gate::new();
            assert!(!g.configure(mac));
            assert!(!g.admit(Some(&AP)));
        }
        let g = Gate::new();
        assert!(g.configure(AP));
        assert!(g.open());
        assert!(!g.admit(None));
        assert!(g.faulted());
        let g = Gate::new();
        assert!(g.configure(AP));
        assert!(g.open());
        g.admitted.store(u32::MAX, Ordering::Relaxed);
        assert!(!g.admit(Some(&AP)));
        assert!(g.faulted());
        assert_eq!(g.snapshot()[1], u32::MAX);
    }
}
