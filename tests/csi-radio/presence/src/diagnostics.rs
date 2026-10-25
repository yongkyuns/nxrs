//! Named, bounded evidence capture. No detector/threshold/native numeric changes.
#[cfg(test)]
use crate::calibration::Rejection;
use crate::calibration::{Phase, HISTORY};
use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Mutex;

pub use csi_lab_core::calibration::History;
#[derive(Clone, Copy)]
struct Evidence {
    tag: u32,
    window: u32,
    sequence: u32,
    stack_free_bytes: u32,
    history: History,
}
impl Evidence {
    const ZERO: Self = Self {
        tag: 0,
        window: 0,
        sequence: 0,
        stack_free_bytes: 0,
        history: History::ZERO,
    };
    fn reason(self) -> &'static str {
        match self.tag {
            14 => "training_complete",
            15 => "background_ready",
            16 => "first_rejected",
            _ => "invalid",
        }
    }
    fn write(&self, out: &mut impl Write) -> io::Result<()> {
        let h = &self.history;
        writeln!(out,"PRESENCE_PAIR v=2 tag={} window={} sequence={} reason={} phase={} features={} filled={} index={} rejection={} average={:08x} lower={:08x} upper={:08x} enter={:08x} exit={:08x} stack_free_bytes={}",
            self.tag,self.window,self.sequence,self.reason(),h.phase.name(),h.features,h.filled,h.index,h.rejection.name(),h.average.to_bits(),h.lower.to_bits(),h.upper.to_bits(),h.enter.to_bits(),h.exit.to_bits(),self.stack_free_bytes)?;
        let mut hash = 2166136261u32;
        for word in [
            2,
            self.tag,
            self.window,
            self.sequence,
            h.phase as u32,
            h.features,
            h.filled as u32,
            h.index as u32,
            h.average.to_bits(),
            h.lower.to_bits(),
            h.upper.to_bits(),
            h.enter.to_bits(),
            h.exit.to_bits(),
            self.stack_free_bytes,
            h.rejection as u32,
        ] {
            hash_word(&mut hash, word);
        }
        for start in (0..HISTORY).step_by(8) {
            write!(out, "PRESENCE_PAIR_HISTORY index={start} entries=")?;
            for i in start..(start + 8).min(HISTORY) {
                if i != start {
                    write!(out, ",")?;
                }
                write!(out, "{:08x}:{:08x}", h.sequences[i], h.values[i].to_bits())?;
                hash_word(&mut hash, h.sequences[i]);
                hash_word(&mut hash, h.values[i].to_bits());
            }
            writeln!(out)?;
        }
        writeln!(
            out,
            "PRESENCE_PAIR_END window={} fnv={hash:08x}",
            self.window
        )
    }
}
fn hash_word(hash: &mut u32, word: u32) {
    for byte in word.to_le_bytes() {
        *hash = (*hash ^ u32::from(byte)).wrapping_mul(16777619);
    }
}

pub struct Diagnostics {
    enabled: AtomicBool,
    seen: AtomicU32,
    ready: AtomicBool,
    value: Mutex<Evidence>,
    pub failures: AtomicU32,
}
impl Diagnostics {
    pub const fn new() -> Self {
        Self {
            enabled: AtomicBool::new(false),
            seen: AtomicU32::new(0),
            ready: AtomicBool::new(false),
            value: Mutex::new(Evidence::ZERO),
            failures: AtomicU32::new(0),
        }
    }
    pub fn prepare(&self) -> bool {
        if self.enabled.load(Ordering::Acquire) || self.value.try_lock().is_err() {
            return false;
        }
        self.enabled.store(true, Ordering::Release);
        true
    }
    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Acquire)
    }
    fn fail(&self) {
        self.failures.fetch_add(1, Ordering::Relaxed);
    }
    /// Event priority is monotonic. A first rejection may skip background-ready;
    /// missing earlier evidence is reported explicitly rather than relabelled.
    pub fn capture(
        &self,
        window: u32,
        sequence: u32,
        training_complete: bool,
        h: History,
        retain: impl FnOnce(u32) -> bool,
    ) {
        if !self.enabled() {
            return;
        }
        let tag = if h.phase == Phase::Rejected {
            16
        } else if h.background_ready {
            15
        } else if training_complete {
            14
        } else {
            return;
        };
        let bit = 1u32 << (tag - 14);
        let seen = self.seen.load(Ordering::Acquire);
        if seen & bit != 0 || seen & (!((1 << (tag - 14 + 1)) - 1)) != 0 {
            return;
        }
        // Commit exactly once even if transfer fails; never substitute a later
        // matrix for the first event under the same diagnostic label.
        if self.seen.fetch_or(bit, Ordering::AcqRel) & bit != 0 {
            return;
        }
        let Ok(mut target) = self.value.try_lock() else {
            self.fail();
            return;
        };
        if self.ready.load(Ordering::Acquire) {
            self.fail();
            return;
        }
        if !retain(tag) {
            self.fail();
            return;
        }
        #[cfg(target_os = "espidf")]
        let stack_free_bytes = unsafe {
            extern "C" {
                fn presence_task_stack_free_bytes() -> u32;
            }
            presence_task_stack_free_bytes()
        };
        #[cfg(not(target_os = "espidf"))]
        let stack_free_bytes = 0;
        *target = Evidence {
            tag,
            window,
            sequence,
            stack_free_bytes,
            history: h,
        };
        self.ready.store(true, Ordering::Release);
    }
    pub fn dump(&self, out: &mut impl Write) -> io::Result<()> {
        if !self.ready.load(Ordering::Acquire) {
            return Ok(());
        }
        let Ok(target) = self.value.try_lock() else {
            return Ok(());
        };
        target.write(out)?;
        self.ready.store(false, Ordering::Release);
        Ok(())
    }
}
pub static DIAGNOSTICS: Diagnostics = Diagnostics::new();
#[cfg(target_os = "espidf")]
#[no_mangle]
pub static presence_pair_static_bytes: u32 = std::mem::size_of::<Diagnostics>() as u32;

#[cfg(test)]
mod tests {
    use super::*;
    fn history(phase: Phase) -> History {
        History {
            phase,
            ..History::ZERO
        }
    }
    #[test]
    fn exact_event_identity_is_retained_once_and_failed_write_does_not_discard() {
        let d = Diagnostics::new();
        assert!(d.prepare());
        d.capture(42, 99, true, history(Phase::Background), |tag| tag == 14);
        d.capture(43, 100, true, history(Phase::Background), |_| {
            panic!("later call cannot replace first")
        });
        struct Fail;
        impl Write for Fail {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> {
                Err(io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        assert!(d.dump(&mut Fail).is_err());
        assert!(d.ready.load(Ordering::Acquire));
        let mut out = Vec::new();
        d.dump(&mut out).unwrap();
        assert!(String::from_utf8(out)
            .unwrap()
            .contains("tag=14 window=42 sequence=99"));
    }
    #[test]
    fn source_capture_failure_cannot_be_retried_with_a_later_matrix() {
        let d = Diagnostics::new();
        assert!(d.prepare());
        d.capture(1, 1, true, history(Phase::Rejected), |_| false);
        d.capture(2, 2, true, history(Phase::Rejected), |_| {
            panic!("must not retry first rejection")
        });
        assert_eq!(d.failures.load(Ordering::Relaxed), 1);
        assert!(!d.ready.load(Ordering::Acquire));
    }
    #[test]
    fn paused_reader_and_unconsumed_evidence_do_not_block_the_sdk() {
        let d = Diagnostics::new();
        assert!(d.prepare());
        let paused = d.value.lock().unwrap();
        d.capture(1, 1, true, history(Phase::Background), |_| panic!());
        drop(paused);
        assert_eq!(d.failures.load(Ordering::Relaxed), 1);
        d.capture(
            1,
            1,
            true,
            History {
                background_ready: true,
                ..history(Phase::Background)
            },
            |_| true,
        );
        d.capture(2, 2, true, history(Phase::Rejected), |_| panic!());
        assert_eq!(d.failures.load(Ordering::Relaxed), 2);
    }
    #[test]
    fn history_wire_matches_independent_python_fixture() {
        let mut h = History {
            phase: Phase::Background,
            features: 1,
            filled: 25,
            index: 0,
            average: 7.0 / 1024.0,
            lower: 7.0 / 1024.0,
            upper: 7.0 / 1024.0,
            ..History::ZERO
        };
        for i in 0..25 {
            h.sequences[i] = i as u32 + 1;
            h.values[i] = (i + 1) as f32 / 1024.0;
        }
        let e = Evidence {
            tag: 14,
            window: 7,
            sequence: 25,
            stack_free_bytes: 0,
            history: h,
        };
        let mut out = Vec::new();
        e.write(&mut out).unwrap();
        assert_eq!(
            String::from_utf8(out).unwrap(),
            include_str!("../fixtures/calibration-pair-v2.txt")
        );
        let rejected = Evidence {
            tag: 16,
            history: History {
                phase: Phase::Rejected,
                rejection: Rejection::SourceContention,
                ..h
            },
            ..e
        };
        let mut out = Vec::new();
        rejected.write(&mut out).unwrap();
        assert_eq!(
            String::from_utf8(out).unwrap(),
            include_str!("../fixtures/calibration-rejection-v2.txt")
        );
    }
}
