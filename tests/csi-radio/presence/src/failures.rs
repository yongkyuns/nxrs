//! Opt-in first-anomaly evidence for selected-source versus other SDK calls.
//! All captured calls consume owned inputs. Never substitutes native results.
use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
use std::sync::Mutex;
const WORDS: usize = 1024;
const COLS: usize = 64;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum Source {
    Selected = 0,
    Other = 1,
}
impl Source {
    fn name(self) -> &'static str {
        match self {
            Self::Selected => "selected",
            Self::Other => "other",
        }
    }
}
#[derive(Clone, Copy)]
#[repr(u32)]
enum Reason {
    NativeStatus = 1,
    NonfiniteInput = 2,
    NonfiniteOutput = 3,
}
impl Reason {
    fn name(self) -> &'static str {
        match self {
            Self::NativeStatus => "native_status",
            Self::NonfiniteInput => "nonfinite_input",
            Self::NonfiniteOutput => "nonfinite_output",
        }
    }
}
#[derive(Clone, Copy)]
struct Header {
    source: Source,
    reason: Reason,
    window: u32,
    us: u64,
    cols: usize,
    rows0: usize,
    rows1: usize,
    status: i32,
}
struct Evidence {
    h: Header,
    matrix: [f32; WORDS],
    output: [f32; COLS],
}
impl Evidence {
    const ZERO: Self = Self {
        h: Header {
            source: Source::Selected,
            reason: Reason::NativeStatus,
            window: 0,
            us: 0,
            cols: 0,
            rows0: 0,
            rows1: 0,
            status: 0,
        },
        matrix: [0.0; WORDS],
        output: [0.0; COLS],
    };
    fn copy_from(&mut self, other: &Self) {
        self.h = other.h;
        let n = (self.h.rows0 + self.h.rows1) * self.h.cols;
        self.matrix[..n].copy_from_slice(&other.matrix[..n]);
        if self.h.status == 0 {
            self.output[..self.h.cols].copy_from_slice(&other.output[..self.h.cols]);
        }
    }
    fn write(&self, out: &mut impl Write) -> io::Result<()> {
        let h = self.h;
        writeln!(out,"PRESENCE_NUMERIC_FAILURE v=1 source={} reason={} window={} us={} cols={} rows0={} rows1={} status={}",h.source.name(),h.reason.name(),h.window,h.us,h.cols,h.rows0,h.rows1,h.status)?;
        let mut hash = 2166136261u32;
        let mut word = |v: u32| {
            for b in v.to_le_bytes() {
                hash = (hash ^ u32::from(b)).wrapping_mul(16777619);
            }
        };
        for v in [
            1,
            h.source as u32,
            h.reason as u32,
            h.window,
            h.us as u32,
            (h.us >> 32) as u32,
            h.cols as u32,
            h.rows0 as u32,
            h.rows1 as u32,
            h.status as u32,
        ] {
            word(v);
        }
        for (name, values) in [
            ("matrix", &self.matrix[..(h.rows0 + h.rows1) * h.cols]),
            (
                "output",
                &self.output[..if h.status == 0 { h.cols } else { 0 }],
            ),
        ] {
            for start in (0..values.len()).step_by(16) {
                write!(
                    out,
                    "PRESENCE_FAILURE_WORDS part={name} index={start} bits="
                )?;
                for (i, value) in values[start..(start + 16).min(values.len())]
                    .iter()
                    .enumerate()
                {
                    if i > 0 {
                        write!(out, ",")?;
                    }
                    let bits = value.to_bits();
                    write!(out, "{bits:08x}")?;
                    word(bits);
                }
                writeln!(out)?;
            }
        }
        writeln!(
            out,
            "PRESENCE_NUMERIC_FAILURE_END window={} fnv={hash:08x}",
            h.window
        )
    }
}
struct Counts {
    calls: AtomicU32,
    bad_input: AtomicU32,
    bad_output: AtomicU32,
    errors: AtomicU32,
    seen: AtomicBool,
}
impl Counts {
    const fn new() -> Self {
        Self {
            calls: AtomicU32::new(0),
            bad_input: AtomicU32::new(0),
            bad_output: AtomicU32::new(0),
            errors: AtomicU32::new(0),
            seen: AtomicBool::new(false),
        }
    }
}
struct Slot {
    value: Mutex<Evidence>,
    ready: AtomicBool,
    delivered: AtomicBool,
}
impl Slot {
    const fn new() -> Self {
        Self {
            value: Mutex::new(Evidence::ZERO),
            ready: AtomicBool::new(false),
            delivered: AtomicBool::new(false),
        }
    }
}
pub struct Failures {
    enabled: AtomicBool,
    owner: AtomicUsize,
    work: Mutex<Evidence>,
    slots: [Slot; 2],
    counts: [Counts; 2],
    losses: AtomicU32,
}
impl Failures {
    pub const fn new() -> Self {
        Self {
            enabled: AtomicBool::new(false),
            owner: AtomicUsize::new(0),
            work: Mutex::new(Evidence::ZERO),
            slots: [Slot::new(), Slot::new()],
            counts: [Counts::new(), Counts::new()],
            losses: AtomicU32::new(0),
        }
    }
    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Acquire)
    }
    pub fn prepare(&self) -> bool {
        if self.enabled()
            || self.work.try_lock().is_err()
            || self.slots.iter().any(|s| s.value.try_lock().is_err())
        {
            return false;
        }
        self.enabled.store(true, Ordering::Release);
        true
    }
    pub fn uninspected(&self) {
        if self.enabled() {
            self.lost();
        }
    }
    fn lost(&self) {
        self.losses.fetch_add(1, Ordering::Relaxed);
    }
    /// SDK owns valid slices/output for this synchronous call. The evidence owns
    /// every input actually consumed; no pre/post hash can prove borrowed stability.
    pub unsafe fn compute(
        &self,
        source: Option<Source>,
        task: usize,
        window: u32,
        us: u64,
        cols: usize,
        rows0: usize,
        rows1: usize,
        a: &[f32],
        b: &[f32],
        output: *mut f32,
        mut call: impl FnMut(*const f32, *const f32) -> i32,
    ) -> i32 {
        if !self.enabled() {
            return call(a.as_ptr(), b.as_ptr());
        }
        let Some(source) = source else {
            self.lost();
            return call(a.as_ptr(), b.as_ptr());
        };
        let counts = &self.counts[source as usize];
        counts.calls.fetch_add(1, Ordering::Release);
        let owner = self.owner.load(Ordering::Acquire);
        if task == 0
            || (owner != task
                && !(owner == 0
                    && self
                        .owner
                        .compare_exchange(0, task, Ordering::AcqRel, Ordering::Relaxed)
                        .is_ok()))
        {
            self.lost();
            return call(a.as_ptr(), b.as_ptr());
        }
        if window == 0
            || cols == 0
            || cols > COLS
            || rows0
                .checked_add(rows1)
                .and_then(|r| r.checked_mul(cols))
                .is_none_or(|n| n == 0 || n > WORDS)
            || a.len() != rows0 * cols
            || b.len() != rows1 * cols
            || output.is_null()
        {
            self.lost();
            return call(a.as_ptr(), b.as_ptr());
        }
        let Ok(mut work) = self.work.try_lock() else {
            self.lost();
            return call(a.as_ptr(), b.as_ptr());
        };
        work.h = Header {
            source,
            reason: Reason::NativeStatus,
            window,
            us,
            cols,
            rows0,
            rows1,
            status: 0,
        };
        work.matrix[..a.len()].copy_from_slice(a);
        work.matrix[a.len()..a.len() + b.len()].copy_from_slice(b);
        let input_ok = work.matrix[..a.len() + b.len()]
            .iter()
            .all(|v| v.is_finite());
        let status = call(work.matrix.as_ptr(), work.matrix.as_ptr().add(a.len()));
        work.h.status = status;
        let output_ok = if status == 0 {
            work.output[..cols].copy_from_slice(std::slice::from_raw_parts(output, cols));
            work.output[..cols].iter().all(|v| v.is_finite())
        } else {
            true
        };
        if !input_ok {
            counts.bad_input.fetch_add(1, Ordering::Release);
        }
        if !output_ok {
            counts.bad_output.fetch_add(1, Ordering::Release);
        }
        if status != 0 {
            counts.errors.fetch_add(1, Ordering::Release);
        }
        let reason = if status != 0 {
            Some(Reason::NativeStatus)
        } else if !input_ok {
            Some(Reason::NonfiniteInput)
        } else if !output_ok {
            Some(Reason::NonfiniteOutput)
        } else {
            None
        };
        if let Some(reason) = reason {
            // Commit first anomaly before handoff: lost evidence never retries later.
            if !counts.seen.swap(true, Ordering::AcqRel) {
                work.h.reason = reason;
                let slot = &self.slots[source as usize];
                if let Ok(mut target) = slot.value.try_lock() {
                    target.copy_from(&work);
                    slot.ready.store(true, Ordering::Release);
                } else {
                    self.lost();
                }
            }
        }
        status
    }
    pub fn dump(&self, out: &mut impl Write) -> io::Result<()> {
        for source in [Source::Selected, Source::Other] {
            let slot = &self.slots[source as usize];
            if slot.ready.load(Ordering::Acquire) {
                if let Ok(value) = slot.value.try_lock() {
                    value.write(out)?;
                    slot.ready.store(false, Ordering::Release);
                    slot.delivered.store(true, Ordering::Release);
                }
            }
            let c = &self.counts[source as usize];
            let load = |v: &AtomicU32| v.load(Ordering::Acquire);
            // Read event counters before calls: the writer increments calls
            // first, so a concurrent call cannot make this report overcount.
            let input = load(&c.bad_input);
            let output = load(&c.bad_output);
            let errors = load(&c.errors);
            let calls = load(&c.calls);
            writeln!(out,"PRESENCE_FAILURE_STATUS source={} calls={} bad_input={} bad_output={} native_errors={} captured={} losses={}",source.name(),calls,input,output,errors,u8::from(slot.delivered.load(Ordering::Acquire)),load(&self.losses))?;
        }
        Ok(())
    }
}
pub static FAILURES: Failures = Failures::new();
#[cfg(target_os = "espidf")]
#[no_mangle]
pub static presence_failure_static_bytes: u32 = (std::mem::size_of::<Failures>()
    + std::mem::size_of::<crate::failure_queue::Queue>()
    + std::mem::size_of::<crate::source_gate::Gate>())
    as u32;
#[cfg(test)]
mod tests {
    use super::*;
    unsafe fn invoke(
        f: &Failures,
        s: Source,
        window: u32,
        output: *mut f32,
        call: impl FnMut(*const f32, *const f32) -> i32,
    ) -> i32 {
        f.compute(
            Some(s),
            7,
            window,
            99,
            2,
            1,
            0,
            &[1.0, 2.0],
            &[],
            output,
            call,
        )
    }
    #[test]
    fn failure_wire_matches_independent_python_fixture_and_failed_drain_retains_it() {
        let f = Failures::new();
        assert!(f.prepare());
        let mut out = [0.0; 2];
        unsafe {
            f.compute(
                Some(Source::Other),
                7,
                7,
                99,
                2,
                1,
                0,
                &[1.0, 2.0],
                &[],
                out.as_mut_ptr(),
                |_, _| {
                    out = [f32::from_bits(0x7fc01234), 1.0];
                    0
                },
            );
        }
        struct Fail;
        impl Write for Fail {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> {
                Err(io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        assert!(f.dump(&mut Fail).is_err());
        assert!(f.slots[1].ready.load(Ordering::Acquire));
        let mut text = Vec::new();
        f.dump(&mut text).unwrap();
        let text = String::from_utf8(text).unwrap();
        let fixture = include_str!("../fixtures/numeric-failure-v1.txt");
        assert!(text.contains(fixture));
        assert!(f.slots[1].delivered.load(Ordering::Acquire));
    }
    #[test]
    fn success_nan_is_retained_once_per_source_and_never_changes_return() {
        let f = Failures::new();
        assert!(f.prepare());
        let mut out = [0.0; 2];
        for source in [Source::Selected, Source::Other] {
            for window in 1..=2 {
                assert_eq!(
                    unsafe {
                        invoke(&f, source, window, out.as_mut_ptr(), |_, _| {
                            out = [f32::from_bits(0x7fc01234), 1.0];
                            0
                        })
                    },
                    0
                );
            }
        }
        let mut text = Vec::new();
        f.dump(&mut text).unwrap();
        let text = String::from_utf8(text).unwrap();
        assert_eq!(text.matches("PRESENCE_NUMERIC_FAILURE v=1").count(), 2);
        assert_eq!(text.matches("window=1 us=99").count(), 2);
        assert!(text.contains("bad_output=2 native_errors=0 captured=1 losses=0"));
        assert!(text.contains("7fc01234"));
    }
    #[test]
    fn call_consumes_owned_copies_and_retained_input_survives_source_reuse() {
        let f = Failures::new();
        assert!(f.prepare());
        let mut input = [1.0, 2.0];
        let mut out = [0.0; 2];
        let original = input.as_ptr();
        unsafe {
            f.compute(
                Some(Source::Other),
                7,
                1,
                0,
                2,
                1,
                0,
                &input,
                &[],
                out.as_mut_ptr(),
                |a, _| {
                    assert_ne!(a, original);
                    assert_eq!(*a, 1.0);
                    out = [f32::NAN, 1.0];
                    0
                },
            );
        }
        input[0] = 99.0;
        assert_eq!(input[0], 99.0);
        let mut text = Vec::new();
        f.dump(&mut text).unwrap();
        assert!(String::from_utf8(text)
            .unwrap()
            .contains("bits=3f800000,40000000"));
    }
    #[test]
    fn native_failure_does_not_read_output_or_retry_lost_first_anomaly() {
        let f = Failures::new();
        assert!(f.prepare());
        let locked = f.slots[0].value.lock().unwrap();
        let mut out = [123.0; 2];
        assert_eq!(
            unsafe { invoke(&f, Source::Selected, 1, out.as_mut_ptr(), |_, _| -1) },
            -1
        );
        drop(locked);
        assert_eq!(
            unsafe { invoke(&f, Source::Selected, 2, out.as_mut_ptr(), |_, _| -1) },
            -1
        );
        let mut text = Vec::new();
        f.dump(&mut text).unwrap();
        let text = String::from_utf8(text).unwrap();
        assert!(!text.contains("PRESENCE_NUMERIC_FAILURE v=1"));
        assert!(text.contains("native_errors=2 captured=0 losses=1"));
        assert_eq!(out, [123.0; 2]);
    }
    #[test]
    fn finite_control_has_no_failure_and_disabled_case_preserves_pointers() {
        let f = Failures::new();
        let input = [1.0, 2.0];
        let mut out = [1.0, 2.0];
        unsafe {
            f.compute(
                Some(Source::Selected),
                7,
                1,
                0,
                2,
                1,
                0,
                &input,
                &[],
                out.as_mut_ptr(),
                |a, _| {
                    assert_eq!(a, input.as_ptr());
                    0
                },
            );
        }
        assert!(f.prepare());
        unsafe {
            invoke(&f, Source::Selected, 1, out.as_mut_ptr(), |_, _| 0);
        }
        let mut text = Vec::new();
        f.dump(&mut text).unwrap();
        assert!(!String::from_utf8(text)
            .unwrap()
            .contains("PRESENCE_NUMERIC_FAILURE v=1"));
    }
    #[test]
    fn foreign_task_and_contention_report_loss_without_blocking() {
        let f = Failures::new();
        assert!(f.prepare());
        let mut out = [1.0, 2.0];
        unsafe {
            invoke(&f, Source::Selected, 1, out.as_mut_ptr(), |_, _| 0);
        }
        let locked = f.work.lock().unwrap();
        unsafe {
            invoke(&f, Source::Other, 2, out.as_mut_ptr(), |_, _| 0);
        }
        drop(locked);
        unsafe {
            f.compute(
                Some(Source::Other),
                8,
                3,
                0,
                2,
                1,
                0,
                &[1.0, 2.0],
                &[],
                out.as_mut_ptr(),
                |_, _| 0,
            );
        }
        assert_eq!(f.losses.load(Ordering::Acquire), 2);
        f.uninspected();
        assert_eq!(f.losses.load(Ordering::Acquire), 3);
    }
}

#[cfg(target_os = "espidf")]
mod native {
    use super::*;
    use crate::failure_queue::QUEUE;
    extern "C" {
        fn presence_task_identity() -> *mut std::ffi::c_void;
        fn esp_radar_motion_dec_compute(
            cols: u32,
            rows0: u32,
            a: *const f32,
            rows1: u32,
            b: *const f32,
            output: *mut f32,
        ) -> i32;
    }
    #[no_mangle]
    pub extern "C" fn presence_failure_active() -> bool {
        FAILURES.enabled() && QUEUE.enabled()
    }
    #[no_mangle]
    pub unsafe extern "C" fn presence_failure_enqueue(
        cols: u32,
        begin: u32,
        count: u32,
        ring: u32,
        rows: *const *const f32,
    ) -> u32 {
        if rows.is_null() || ring == 0 || ring > 1024 || cols == 0 || cols > 64 {
            QUEUE.losses.fetch_add(1, Ordering::Relaxed);
            return 0;
        }
        let pointers = std::slice::from_raw_parts(rows, ring as usize);
        QUEUE.enqueue(
            presence_task_identity() as usize,
            cols as usize,
            begin as usize,
            count as usize,
            ring as usize,
            |i| {
                let p = pointers[i];
                if p.is_null() {
                    None
                } else {
                    Some(std::slice::from_raw_parts(p, cols as usize))
                }
            },
        )
    }
    #[no_mangle]
    pub unsafe extern "C" fn presence_failure_compute(
        token: u32,
        cols: u32,
        rows0: u32,
        rows1: u32,
        output: *mut f32,
    ) -> i32 {
        if output.is_null() {
            QUEUE.losses.fetch_add(1, Ordering::Relaxed);
            return 0x102;
        }
        QUEUE.compute(
            presence_task_identity() as usize,
            token,
            cols as usize,
            rows0 as usize,
            rows1 as usize,
            |a, b| esp_radar_motion_dec_compute(cols, rows0, a, rows1, b, output),
        )
    }
}
