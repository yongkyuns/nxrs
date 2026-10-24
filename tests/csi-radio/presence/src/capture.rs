//! Optional frozen-input experiment. Fixed storage; SDK workers only try-lock.
//! The unchanged vendor calculation receives the exact owned input exported.
//! No recording output, allocation or waiting occurs on the SDK workers.
use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
use std::sync::Mutex;

pub const COLS: usize = 64;
pub const WORDS: usize = 1024;
pub const TEMPLATES: usize = 10;
const TAGS: u32 = 16;

#[derive(Clone, Copy)]
pub struct Gain {
    pub sequence: u32,
    pub agc: u8,
    pub fft: i8,
    pub status: i32,
    pub factor: f32,
}
impl Gain {
    const ZERO: Self = Self {
        sequence: 0,
        agc: 0,
        fft: 0,
        status: 0,
        factor: 0.0,
    };
}
#[derive(Clone, Copy)]
struct Header {
    tag: u32,
    window: u32,
    us: u64,
    cols: usize,
    rows0: usize,
    rows1: usize,
    status: i32,
    templates: usize,
    gain: Gain,
}
impl Header {
    const ZERO: Self = Self {
        tag: 0,
        window: 0,
        us: 0,
        cols: 0,
        rows0: 0,
        rows1: 0,
        status: 0,
        templates: 0,
        gain: Gain::ZERO,
    };
    fn words(self) -> [u32; 15] {
        [
            self.tag,
            self.window,
            self.us as u32,
            (self.us >> 32) as u32,
            self.cols as u32,
            self.rows0 as u32,
            self.rows1 as u32,
            self.status as u32,
            self.templates as u32,
            self.gain.sequence,
            self.gain.agc as u32,
            self.gain.fft as i32 as u32,
            self.gain.status as u32,
            self.gain.factor.to_bits(),
            1,
        ]
    }
}
struct Snapshot {
    header: Header,
    matrix: [f32; WORDS],
    output: [f32; COLS],
    templates: [f32; TEMPLATES * COLS],
    wander: [f32; TEMPLATES],
}
impl Snapshot {
    const ZERO: Self = Self {
        header: Header::ZERO,
        matrix: [0.0; WORDS],
        output: [0.0; COLS],
        templates: [0.0; TEMPLATES * COLS],
        wander: [0.0; TEMPLATES],
    };
    fn parts(&self) -> [(&'static str, &[f32]); 4] {
        let h = self.header;
        [
            ("matrix", &self.matrix[..(h.rows0 + h.rows1) * h.cols]),
            (
                "output",
                &self.output[..if h.status == 0 { h.cols } else { 0 }],
            ),
            ("templates", &self.templates[..h.templates * h.cols]),
            ("wander", &self.wander[..h.templates]),
        ]
    }
    fn copy_from(&mut self, source: &Self) {
        self.header = source.header;
        let [(_, matrix), (_, output), (_, templates), (_, wander)] = source.parts();
        self.matrix[..matrix.len()].copy_from_slice(matrix);
        self.output[..output.len()].copy_from_slice(output);
        self.templates[..templates.len()].copy_from_slice(templates);
        self.wander[..wander.len()].copy_from_slice(wander);
    }
    fn write(&self, kind: &str, out: &mut impl Write) -> io::Result<()> {
        let h = self.header;
        writeln!(out,"PRESENCE_SNAPSHOT v=1 kind={kind} tag={} window={} us={} cols={} rows0={} rows1={} status={} templates={} gain_seq={} agc={} fft={} gain_status={} factor={:08x}",
            h.tag,h.window,h.us,h.cols,h.rows0,h.rows1,h.status,h.templates,h.gain.sequence,
            h.gain.agc,h.gain.fft,h.gain.status,h.gain.factor.to_bits())?;
        let mut hash = 2166136261;
        for word in h.words() {
            hash_word(&mut hash, word);
        }
        for (name, values) in self.parts() {
            for (index, chunk) in values.chunks(16).enumerate() {
                write!(out, "PRESENCE_WORDS part={name} index={} bits=", index * 16)?;
                for (i, value) in chunk.iter().enumerate() {
                    if i != 0 {
                        write!(out, ",")?;
                    }
                    write!(out, "{:08x}", value.to_bits())?;
                    hash_word(&mut hash, value.to_bits());
                }
                writeln!(out)?;
            }
        }
        writeln!(
            out,
            "PRESENCE_SNAPSHOT_END window={} fnv={hash:08x}",
            h.window
        )
    }
}
fn hash_word(hash: &mut u32, word: u32) {
    for byte in word.to_le_bytes() {
        *hash = (*hash ^ u32::from(byte)).wrapping_mul(16777619);
    }
}
struct Slot {
    value: Mutex<Snapshot>,
    ready: AtomicBool,
}
impl Slot {
    const fn new() -> Self {
        Self {
            value: Mutex::new(Snapshot::ZERO),
            ready: AtomicBool::new(false),
        }
    }
    fn publish(&self, value: &Snapshot) -> bool {
        if self.ready.load(Ordering::Acquire) {
            return false;
        }
        let Ok(mut target) = self.value.try_lock() else {
            return false;
        };
        if self.ready.load(Ordering::Relaxed) {
            return false;
        }
        target.copy_from(value);
        self.ready.store(true, Ordering::Release);
        true
    }
    fn dump(&self, kind: &str, out: &mut impl Write) -> io::Result<bool> {
        if !self.ready.load(Ordering::Acquire) {
            return Ok(false);
        }
        let Ok(value) = self.value.try_lock() else {
            return Ok(false);
        };
        if !self.ready.load(Ordering::Relaxed) {
            return Ok(false);
        }
        value.write(kind, out)?;
        self.ready.store(false, Ordering::Release);
        Ok(true)
    }
}
struct Work {
    snapshot: Snapshot,
    active: bool,
    valid: bool,
    output_address: usize,
}
impl Work {
    const ZERO: Self = Self {
        snapshot: Snapshot::ZERO,
        active: false,
        valid: false,
        output_address: 0,
    };
}
pub struct Recorder {
    pub enabled: AtomicBool,
    owner: AtomicUsize,
    phase: AtomicU32,
    pending: AtomicU32,
    last_tag: AtomicU32,
    work: Mutex<Work>,
    sample: Slot,
    failure: Slot,
    first_failure: AtomicBool,
    gain: Mutex<Gain>,
    gain_sequence: AtomicU32,
    pub skipped: AtomicU32,
    pub foreign: AtomicU32,
    pub invalid: AtomicU32,
}
impl Recorder {
    pub const fn new() -> Self {
        Self {
            enabled: AtomicBool::new(false),
            owner: AtomicUsize::new(0),
            phase: AtomicU32::new(0),
            pending: AtomicU32::new(0),
            last_tag: AtomicU32::new(0),
            work: Mutex::new(Work::ZERO),
            sample: Slot::new(),
            failure: Slot::new(),
            first_failure: AtomicBool::new(true),
            gain: Mutex::new(Gain::ZERO),
            gain_sequence: AtomicU32::new(0),
            skipped: AtomicU32::new(0),
            foreign: AtomicU32::new(0),
            invalid: AtomicU32::new(0),
        }
    }
    /// ESP-IDF's pthread mutexes initialize lazily. Touch each on the control
    /// task before enabling capture so SDK workers never perform that setup.
    pub fn prepare(&self) -> bool {
        if self.enabled.load(Ordering::Acquire) {
            return false;
        }
        let work = self.work.try_lock().is_ok();
        let sample = self.sample.value.try_lock().is_ok();
        let failure = self.failure.value.try_lock().is_ok();
        let gain = self.gain.try_lock().is_ok();
        work && sample && failure && gain
    }
    pub fn arm(&self, tag: u32) -> bool {
        if !self.enabled.load(Ordering::Acquire)
            || tag == 0
            || tag > TAGS
            || tag <= self.last_tag.load(Ordering::Relaxed)
        {
            return false;
        }
        // One host controller owns arm calls; publishing blocks reuse until drain.
        if self.sample.ready.load(Ordering::Acquire)
            || self
                .pending
                .compare_exchange(0, tag, Ordering::AcqRel, Ordering::Relaxed)
                .is_err()
        {
            return false;
        }
        self.last_tag.store(tag, Ordering::Relaxed);
        self.phase.store(tag, Ordering::Release);
        true
    }
    fn owner(&self, task: usize) -> bool {
        if task == 0 {
            self.foreign.fetch_add(1, Ordering::Relaxed);
            return false;
        }
        let current = self.owner.load(Ordering::Acquire);
        if current == task
            || (current == 0
                && self
                    .owner
                    .compare_exchange(0, task, Ordering::AcqRel, Ordering::Relaxed)
                    .is_ok())
        {
            return true;
        }
        self.foreign.fetch_add(1, Ordering::Relaxed);
        false
    }
    fn finish(&self, work: &mut Work) {
        if !work.active {
            return;
        }
        work.active = false;
        if !work.valid || self.foreign.load(Ordering::Relaxed) != 0 {
            return;
        }
        let snap = &work.snapshot;
        if snap.header.status != 0 && self.first_failure.load(Ordering::Relaxed) {
            if self.failure.publish(snap) {
                self.first_failure.store(false, Ordering::Relaxed);
            } else {
                self.skipped.fetch_add(1, Ordering::Relaxed);
            }
        }
        if snap.header.status == 0
            && snap.header.tag != 0
            && self.pending.load(Ordering::Acquire) == snap.header.tag
        {
            if self.sample.publish(snap) {
                let _ = self.pending.compare_exchange(
                    snap.header.tag,
                    0,
                    Ordering::AcqRel,
                    Ordering::Relaxed,
                );
            } else {
                self.skipped.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
    /// Input slices must match the supplied shape; `output` is valid for cols
    /// floats, initialized by `call` on success. No output is read on failure.
    pub unsafe fn motion(
        &self,
        task: usize,
        window: u32,
        us: u64,
        cols: usize,
        rows0: usize,
        rows1: usize,
        data0: &[f32],
        data1: &[f32],
        output: *mut f32,
        mut call: impl FnMut(*const f32, *const f32) -> i32,
    ) -> i32 {
        if !self.owner(task) {
            return call(data0.as_ptr(), data1.as_ptr());
        }
        let Ok(mut work) = self.work.try_lock() else {
            self.skipped.fetch_add(1, Ordering::Relaxed);
            return call(data0.as_ptr(), data1.as_ptr());
        };
        self.finish(&mut work);
        let shape = rows0.checked_add(rows1).and_then(|r| r.checked_mul(cols));
        if cols == 0
            || cols > COLS
            || shape.is_none_or(|n| n == 0 || n > WORDS)
            || data0.len() != rows0 * cols
            || data1.len() != rows1 * cols
            || output.is_null()
        {
            self.invalid.fetch_add(1, Ordering::Relaxed);
            return call(data0.as_ptr(), data1.as_ptr());
        }
        let gain = match self.gain.try_lock() {
            Ok(g) => *g,
            Err(_) => {
                self.skipped.fetch_add(1, Ordering::Relaxed);
                Gain::ZERO
            }
        };
        work.snapshot.header = Header {
            // Trace admission is committed after this recorder is armed. The
            // trace owner latches a tag for the entire numerical call, so an
            // application-task arm cannot retag a preempted/inflight window.
            tag: if crate::trace::TRACE.enabled.load(Ordering::Acquire) {
                crate::trace::TRACE.current_tag()
            } else {
                self.phase.load(Ordering::Acquire)
            },
            window,
            us,
            cols,
            rows0,
            rows1,
            status: 0,
            templates: 0,
            gain,
        };
        work.snapshot.matrix[..data0.len()].copy_from_slice(data0);
        work.snapshot.matrix[data0.len()..data0.len() + data1.len()].copy_from_slice(data1);
        work.active = true;
        work.valid = true;
        work.output_address = output as usize;
        let status = call(
            work.snapshot.matrix.as_ptr(),
            work.snapshot.matrix.as_ptr().add(data0.len()),
        );
        work.snapshot.header.status = status;
        if status == 0 {
            work.snapshot.output[..cols].copy_from_slice(std::slice::from_raw_parts(output, cols));
        } else {
            self.finish(&mut work);
        }
        status
    }
    pub fn wander(
        &self,
        task: usize,
        a: &[f32],
        b: &[f32],
        mut call: impl FnMut(*const f32, *const f32) -> f32,
    ) -> f32 {
        if !self.owner(task) {
            return call(a.as_ptr(), b.as_ptr());
        }
        let Ok(mut work) = self.work.try_lock() else {
            self.skipped.fetch_add(1, Ordering::Relaxed);
            return call(a.as_ptr(), b.as_ptr());
        };
        let h = work.snapshot.header;
        if !work.active
            || !work.valid
            || h.status != 0
            || a.len() != h.cols
            || b.len() != h.cols
            || b.as_ptr() as usize != work.output_address
            || h.templates >= TEMPLATES
            || b.iter()
                .zip(&work.snapshot.output)
                .any(|(a, b)| a.to_bits() != b.to_bits())
        {
            work.valid = false;
            self.invalid.fetch_add(1, Ordering::Relaxed);
            return call(a.as_ptr(), b.as_ptr());
        }
        let start = h.templates * h.cols;
        work.snapshot.templates[start..start + h.cols].copy_from_slice(a);
        let value = call(
            work.snapshot.templates[start..].as_ptr(),
            work.snapshot.output.as_ptr(),
        );
        work.snapshot.wander[h.templates] = value;
        work.snapshot.header.templates += 1;
        value
    }
    pub fn gain(&self, agc: u8, fft: i8, status: i32, factor: f32) {
        let sequence = self.gain_sequence.fetch_add(1, Ordering::Relaxed) + 1;
        if let Ok(mut g) = self.gain.try_lock() {
            *g = Gain {
                sequence,
                agc,
                fft,
                status,
                factor,
            };
        } else {
            self.skipped.fetch_add(1, Ordering::Relaxed);
        }
    }
    pub fn dump(&self, out: &mut impl Write) -> io::Result<()> {
        self.failure.dump("failure", out)?;
        self.sample.dump("sample", out)?;
        Ok(())
    }
}
pub static RECORDER: Recorder = Recorder::new();

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn paused_work_reader_uses_original_call_and_reports_loss() {
        let r = Recorder::new();
        let paused = r.work.lock().unwrap();
        let input = [1.0, 2.0];
        let mut output = [0.0; 2];
        let result = unsafe {
            r.motion(
                1,
                1,
                1,
                2,
                1,
                0,
                &input,
                &[],
                output.as_mut_ptr(),
                |a, _| {
                    assert_eq!(a, input.as_ptr());
                    -1
                },
            )
        };
        assert_eq!(result, -1);
        assert_eq!(r.skipped.load(Ordering::Relaxed), 1);
        drop(paused);
    }
    #[test]
    fn failed_call_never_reads_uninitialized_output_and_failed_drain_retains_slot() {
        let r = Recorder::new();
        let input = [1.0, 2.0];
        let mut output = std::mem::MaybeUninit::<[f32; 2]>::uninit();
        unsafe {
            r.motion(
                1,
                1,
                1,
                2,
                1,
                0,
                &input,
                &[],
                output.as_mut_ptr().cast(),
                |_, _| -1,
            );
        }
        struct Failed;
        impl Write for Failed {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> {
                Err(io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        assert!(r.dump(&mut Failed).is_err());
        assert!(r.failure.ready.load(Ordering::Acquire));
        let mut bytes = Vec::new();
        r.dump(&mut bytes).unwrap();
        assert!(!String::from_utf8(bytes).unwrap().contains("part=output"));
        assert!(!r.failure.ready.load(Ordering::Acquire));
    }
    #[test]
    fn frozen_inputs_outputs_and_templates_are_exact_and_failure_is_retained_once() {
        let r = Recorder::new();
        r.enabled.store(true, Ordering::Release);
        assert!(r.arm(1));
        let input = [1.0, 2.0, 3.0, 4.0];
        let mut output = [0.0; 2];
        r.gain(32, -7, 0, 0.5);
        unsafe {
            r.motion(
                1,
                1,
                10,
                2,
                1,
                1,
                &input[..2],
                &input[2..],
                output.as_mut_ptr(),
                |a, b| {
                    assert_ne!(a, input.as_ptr());
                    assert_eq!(*a, 1.0);
                    assert_eq!(*b, 3.0);
                    output.as_mut_ptr().write(2.0);
                    output.as_mut_ptr().add(1).write(3.0);
                    0
                },
            );
        }
        assert_eq!(
            r.wander(1, &[4.0, 5.0], &output, |a, b| unsafe {
                assert_eq!(*a, 4.0);
                assert_eq!(*b, 2.0);
                0.25
            }),
            0.25
        );
        unsafe {
            r.motion(
                1,
                2,
                11,
                2,
                2,
                0,
                &input,
                &[],
                output.as_mut_ptr(),
                |_, _| -1,
            );
        }
        let mut dump = Vec::new();
        r.dump(&mut dump).unwrap();
        let text = String::from_utf8(dump).unwrap();
        assert_eq!(text, include_str!("../fixtures/snapshot-v1.txt"));
        assert!(text.contains("kind=failure tag=1 window=2"));
        assert!(text.contains("kind=sample tag=1 window=1"));
        assert!(text.contains("part=wander index=0 bits=3e800000"));
        assert!(text.contains("agc=32 fft=-7"));
        unsafe {
            r.motion(
                1,
                3,
                12,
                2,
                2,
                0,
                &input,
                &[],
                output.as_mut_ptr(),
                |_, _| -1,
            );
        }
        let mut next = Vec::new();
        r.dump(&mut next).unwrap();
        assert!(next.is_empty());
        assert_eq!(r.invalid.load(Ordering::Relaxed), 0);
    }
    #[test]
    fn arm_is_bounded_and_does_not_overwrite_an_unconsumed_sample() {
        let r = Recorder::new();
        assert!(r.prepare());
        assert!(!r.arm(1));
        r.enabled.store(true, Ordering::Release);
        assert!(!r.prepare());
        assert!(!r.arm(0));
        assert!(!r.arm(17));
        assert!(r.arm(1));
        assert!(!r.arm(2));
        r.pending.store(0, Ordering::Relaxed);
        r.sample.ready.store(true, Ordering::Release);
        assert!(!r.arm(2));
        r.sample.ready.store(false, Ordering::Release);
        assert!(!r.arm(1));
        assert!(r.arm(2));
    }
    #[test]
    fn paused_consumer_cannot_block_sdk_producer() {
        let r = Recorder::new();
        r.enabled.store(true, Ordering::Release);
        assert!(r.arm(1));
        let paused_consumer = r.sample.value.lock().unwrap();
        let input = [1.0, 2.0];
        let mut output = [0.0; 2];
        unsafe {
            r.motion(1, 1, 1, 2, 1, 0, &input, &[], output.as_mut_ptr(), |_, _| 0);
            r.motion(
                1,
                2,
                2,
                2,
                1,
                0,
                &input,
                &[],
                output.as_mut_ptr(),
                |_, _| -1,
            );
        }
        assert_eq!(r.skipped.load(Ordering::Relaxed), 1);
        assert_eq!(r.pending.load(Ordering::Acquire), 1);
        drop(paused_consumer);
    }
    #[test]
    fn foreign_task_or_wrong_frame_cannot_publish_a_matched_snapshot() {
        let r = Recorder::new();
        r.enabled.store(true, Ordering::Release);
        assert!(r.arm(1));
        let input = [1.0, 2.0];
        let mut output = [0.0; 2];
        unsafe {
            r.motion(1, 1, 1, 2, 1, 0, &input, &[], output.as_mut_ptr(), |_, _| 0);
        }
        r.wander(1, &input, &[3.0, 4.0], |_, _| 0.5);
        unsafe {
            r.motion(
                2,
                2,
                2,
                2,
                1,
                0,
                &input,
                &[],
                output.as_mut_ptr(),
                |_, _| -1,
            );
        }
        unsafe {
            r.motion(
                1,
                3,
                3,
                2,
                1,
                0,
                &input,
                &[],
                output.as_mut_ptr(),
                |_, _| -1,
            );
        }
        let mut dump = Vec::new();
        r.dump(&mut dump).unwrap();
        assert!(dump.is_empty());
        assert!(r.foreign.load(Ordering::Relaxed) > 0);
        assert!(r.invalid.load(Ordering::Relaxed) > 0);
    }
    #[test]
    fn invalid_shape_falls_back_without_reading_failed_output() {
        let r = Recorder::new();
        let data = [0.0; 65];
        let status = unsafe {
            r.motion(
                1,
                1,
                1,
                65,
                1,
                0,
                &data,
                &[],
                std::ptr::null_mut(),
                |_, _| -1,
            )
        };
        assert_eq!(status, -1);
        assert_eq!(r.invalid.load(Ordering::Relaxed), 1);
    }
}
