//! Optional post-main observation of the shared ESP32-S3 stack.
//!
//! This is an observed watermark, never a worst-case stack bound. The first
//! paint happens after the Rust entry prologue, so it cannot recover startup
//! usage. It preserves the HAL's guard at stack-bottom + 64 and all active
//! frames. The unpainted top is conservatively charged as used. Painting is
//! performed only once: later snapshots include earlier profiles, interrupts,
//! console output and the observer itself.

#[cfg(target_os = "none")]
include!(concat!(env!("OUT_DIR"), "/event_stack.rs"));

// The HAL owns the bottom guard. Do not read or overwrite its storage.
const PROTECTED_BOTTOM_BYTES: usize = 128;
// A changed word in this part of our painted region invalidates headroom.
const OBSERVATION_GUARD_BYTES: usize = 64;
// Keep a gap below the sampled SP as well as the ABI's live frame. The target
// paint/scan loops make no calls and never change a1 while using this boundary.
const SP_GAP_BYTES: usize = 256;
const COLOR: u32 = 0xa5a5_a5a5;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Region {
    bottom: usize,
    top: usize,
    start: usize,
    end: usize,
}

impl Region {
    fn new(bottom: usize, top: usize, end: usize) -> Option<Self> {
        let start = bottom.checked_add(PROTECTED_BOTTOM_BYTES)?;
        if bottom % 16 != 0
            || top % 16 != 0
            || end % 16 != 0
            || end > top
            || end.checked_sub(start)? < OBSERVATION_GUARD_BYTES
        {
            return None;
        }
        Some(Self { bottom, top, start, end })
    }

    fn observation(self, first_changed: usize, scan_end: usize) -> Option<Observation> {
        if scan_end > self.end
            || scan_end < self.start
            || scan_end % 16 != 0
            || first_changed < self.start
            || first_changed > scan_end
            || first_changed % 4 != 0
        {
            return None;
        }
        Some(Observation {
            available: true,
            reserved: self.top - self.bottom,
            // If the scan is shortened to avoid the observer's current frame,
            // this also charges that uninspected top as used. All-color data
            // reports the unpainted floor, never a fictitious zero-byte stack.
            used: self.top - first_changed,
            painted_bytes: self.end - self.start,
            unpainted_bytes: self.top - self.end,
            protected_bytes: PROTECTED_BOTTOM_BYTES,
            guard_ok: first_changed - self.start >= OBSERVATION_GUARD_BYTES,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Observation {
    pub available: bool,
    pub reserved: usize,
    pub used: usize,
    pub painted_bytes: usize,
    pub unpainted_bytes: usize,
    pub protected_bytes: usize,
    pub guard_ok: bool,
}

impl Observation {
    fn unavailable(reserved: usize) -> Self {
        Self {
            available: false,
            reserved,
            used: 0,
            painted_bytes: 0,
            unpainted_bytes: 0,
            protected_bytes: 0,
            guard_ok: false,
        }
    }
}

#[cfg(all(target_os = "none", target_arch = "xtensa"))]
mod target {
    use super::*;
    use core::fmt::Write;

    unsafe extern "C" {
        static _stack_end_cpu0: u8;
        static _stack_start_cpu0: u8;
    }

    pub struct StackWatermark {
        region: Option<Region>,
        reserved: usize,
    }

    impl StackWatermark {
        /// Call once at the beginning of main, before HAL and executor setup.
        /// Linker bounds and the live SP are checked before any memory write.
        pub fn start() -> Self {
            let bottom = core::ptr::addr_of!(_stack_end_cpu0) as usize;
            let top = core::ptr::addr_of!(_stack_start_cpu0) as usize;
            let reserved = top.checked_sub(bottom).unwrap_or(0);
            if !STACK_WATERMARK
                || reserved != SHARED_STACK_BYTES
                || bottom % 16 != 0
                || top % 16 != 0
                || reserved < 1024
            {
                return Self { region: None, reserved };
            }
            let start = bottom + PROTECTED_BOTTOM_BYTES;
            let end: usize;
            // SAFETY: the checked linker interval is the CPU0 stack used by
            // this single-core fixture. The assembly verifies a1 belongs to
            // that interval and paints only [bottom + 128, a1 - 256), rounded
            // down. No call, SP adjustment, or formatter can create a frame
            // below a1 during this loop. All maskable interrupts are excluded
            // until the original PS is restored. The HAL guard is untouched.
            unsafe {
                core::arch::asm!(
                    "rsil {saved_ps}, 15",
                    "mov {sampled_sp}, a1",
                    "bltu {sampled_sp}, {start}, 4f",
                    "bltu {top}, {sampled_sp}, 4f",
                    "addi {end}, {sampled_sp}, -128",
                    "addi {end}, {end}, -128",
                    "movi {mask}, -16",
                    "and {end}, {end}, {mask}",
                    "bgeu {start}, {end}, 4f",
                    "2:",
                    "s32i {color}, {cursor}, 0",
                    "addi {cursor}, {cursor}, 4",
                    "bltu {cursor}, {end}, 2b",
                    "j 3f",
                    "4:",
                    "mov {end}, {start}",
                    "3:",
                    "wsr.ps {saved_ps}",
                    "rsync",
                    saved_ps = out(reg) _,
                    sampled_sp = out(reg) _,
                    end = out(reg) end,
                    mask = out(reg) _,
                    cursor = inout(reg) start => _,
                    start = in(reg) start,
                    top = in(reg) top,
                    color = in(reg) COLOR,
                    options(nostack),
                );
            }
            Self { region: Region::new(bottom, top, end), reserved }
        }

        pub fn snapshot(&self) -> Observation {
            let Some(region) = self.region else {
                return Observation::unavailable(self.reserved);
            };
            let first_changed: usize;
            let scan_end: usize;
            // SAFETY: start() initialized this exact interval. The scan is
            // shortened below the current a1 with the same gap, so it never
            // reads an active frame. Only raw word loads occur while maskable
            // interrupts are excluded; the original PS is always restored.
            unsafe {
                core::arch::asm!(
                    "rsil {saved_ps}, 15",
                    "mov {sampled_sp}, a1",
                    "bltu {sampled_sp}, {cursor}, 5f",
                    "bltu {top}, {sampled_sp}, 5f",
                    "addi {scan_end}, {sampled_sp}, -128",
                    "addi {scan_end}, {scan_end}, -128",
                    "movi {mask}, -16",
                    "and {scan_end}, {scan_end}, {mask}",
                    "bltu {scan_end}, {paint_end}, 2f",
                    "mov {scan_end}, {paint_end}",
                    "2:",
                    "bgeu {cursor}, {scan_end}, 4f",
                    "3:",
                    "l32i {word}, {cursor}, 0",
                    "bne {word}, {color}, 4f",
                    "addi {cursor}, {cursor}, 4",
                    "bltu {cursor}, {scan_end}, 3b",
                    "j 4f",
                    "5:",
                    "movi {scan_end}, 0",
                    "4:",
                    "wsr.ps {saved_ps}",
                    "rsync",
                    saved_ps = out(reg) _,
                    sampled_sp = out(reg) _,
                    scan_end = out(reg) scan_end,
                    mask = out(reg) _,
                    word = out(reg) _,
                    cursor = inout(reg) region.start => first_changed,
                    paint_end = in(reg) region.end,
                    top = in(reg) region.top,
                    color = in(reg) COLOR,
                    options(nostack),
                );
            }
            region.observation(first_changed, scan_end)
                .unwrap_or_else(|| Observation::unavailable(self.reserved))
        }

        /// Emit outside the timed interval. A missing optional observer is
        /// explicit; an enabled observer that cannot establish safe coverage
        /// or whose colored guard was touched fails this invocation.
        pub fn report(&self, console: &mut impl Write) -> bool {
            let row = self.snapshot();
            let capture = if !STACK_WATERMARK { "disabled" } else { "post-main-cumulative" };
            if row.available {
                writeln!(console,
                    "ES_STACK scope=shared id=0 requested={} reserved={} used={} available=1 capture={} painted_bytes={} unpainted_bytes={} protected_bytes={} guard_bytes={} guard_ok={} sp_gap_bytes={}",
                    SHARED_STACK_BYTES, row.reserved, row.used, capture,
                    row.painted_bytes, row.unpainted_bytes, row.protected_bytes,
                    OBSERVATION_GUARD_BYTES, u32::from(row.guard_ok), SP_GAP_BYTES).unwrap();
            } else {
                // Missing coverage is not a damaged guard or observed zero.
                writeln!(console,
                    "ES_STACK scope=shared id=0 requested={} reserved={} used=0 available=0 capture={}",
                    SHARED_STACK_BYTES, row.reserved, capture).unwrap();
            }
            if STACK_WATERMARK && (!row.available || !row.guard_ok) {
                writeln!(console, "ES_FAIL stage=stack_observation").unwrap();
                return false;
            }
            true
        }
    }
}

#[cfg(all(target_os = "none", target_arch = "xtensa"))]
pub use target::StackWatermark;

#[cfg(test)]
mod tests {
    use super::*;

    fn region() -> Region {
        Region::new(0x1000, 0x3000, 0x2d00).unwrap()
    }

    #[test]
    fn all_color_counts_the_unknown_top_as_used() {
        let row = region().observation(0x2d00, 0x2d00).unwrap();
        assert_eq!(row.reserved, 8192);
        assert_eq!(row.used, 768);
        assert_eq!(row.unpainted_bytes, 768);
        assert_eq!(row.painted_bytes + row.unpainted_bytes + row.protected_bytes, row.reserved);
        assert!(row.available && row.guard_ok);
    }

    #[test]
    fn deeper_usage_and_observer_frame_are_both_charged() {
        let row = region().observation(0x2504, 0x2c00).unwrap();
        assert_eq!(row.used, 0xafc);
        let observer_deeper_than_old_watermark = region().observation(0x2400, 0x2400).unwrap();
        assert_eq!(observer_deeper_than_old_watermark.used, 0xc00);
        assert!(observer_deeper_than_old_watermark.guard_ok);
    }

    #[test]
    fn touched_colored_guard_is_not_valid_headroom() {
        let row = region().observation(0x1080, 0x2d00).unwrap();
        assert!(!row.guard_ok);
        assert!(region().observation(0x10c0, 0x2d00).unwrap().guard_ok);
        assert!(!region().observation(0x10bc, 0x2d00).unwrap().guard_ok);
    }

    #[test]
    fn invalid_or_insufficient_coverage_stays_unavailable() {
        for (bottom, top, end) in [
            (0x1001, 0x3000, 0x2d00),
            (0x1000, 0x3000, 0x2d04),
            (0x1000, 0x3000, 0x3010),
            (0x1000, 0x3000, 0x10a0),
            (usize::MAX, 0, 0),
        ] {
            assert!(Region::new(bottom, top, end).is_none());
        }
        for (first, end) in [(0x107c, 0x2d00), (0x2d04, 0x2d00), (0x2001, 0x2d00),
                             (0x2d00, 0x2e00), (0x1080, 0x1070)] {
            assert!(region().observation(first, end).is_none());
        }
        let missing = Observation::unavailable(8192);
        assert!(!missing.available && !missing.guard_ok);
        assert_eq!(missing.used, 0); // available=false makes this unknown, not zero use.
    }
}
