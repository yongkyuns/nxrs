//! Test-only RF -> compensation -> preprocessing -> enqueue-owned input trace.
//! Separate bounded stores; all SDK paths try-lock and never print or allocate.
use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};

pub const COLS: usize = 16;
pub const ROWS: usize = 32;
pub const RING: usize = 64;
pub const QUEUED: usize = 10; // Eight queued, one executing, one producer pending send.
pub const META: usize = 22;
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct Radio {
    pub sequence: u32,
    pub timestamp: u32,
    pub rssi: i32,
    pub agc: u32,
    pub fft: i32,
    pub data_type: u32,
    pub length: u32,
    pub factor: f32,
    pub observed_lo: u32,
    pub observed_hi: u32,
}
#[derive(Clone, Copy, Default)]
pub struct Summary {
    pub hash: u32,
    pub zeros: u32,
    pub bytes: u32,
}
#[derive(Clone, Copy)]
struct Rf {
    address: usize,
    radio: Radio,
    gain_sequence: u32,
    gain_state: i32,
    status: i32,
    factor: f32,
    before: Summary,
    after: Summary,
}
impl Rf {
    const ZERO: Self = Self {
        address: 0,
        radio: Radio {
            sequence: 0,
            timestamp: 0,
            rssi: 0,
            agc: 0,
            fft: 0,
            data_type: 0,
            length: 0,
            factor: 0.0,
            observed_lo: 0,
            observed_hi: 0,
        },
        gain_sequence: 0,
        gain_state: -1,
        status: -1,
        factor: 0.0,
        before: Summary {
            hash: 0,
            zeros: 0,
            bytes: 0,
        },
        after: Summary {
            hash: 0,
            zeros: 0,
            bytes: 0,
        },
    };
}
#[derive(Clone, Copy)]
struct Row {
    valid: bool,
    meta: [u32; META],
    iq: [i16; COLS * 2],
    amplitude: [f32; COLS],
    cols: usize,
}
impl Row {
    const ZERO: Self = Self {
        valid: false,
        meta: [0; META],
        iq: [0; COLS * 2],
        amplitude: [0.0; COLS],
        cols: 0,
    };
}
struct RowStore {
    peer: usize,
    rows: [Row; RING],
}
#[derive(Clone, Copy)]
struct Decision {
    status: i32,
    train: u32,
    before: f32,
    action: u32,
    templates: u32,
    background: u32,
    sum: f32,
    basis: f32,
    wander: f32,
    jitter: f32,
}
impl Decision {
    const ZERO: Self = Self {
        status: -1,
        train: 0,
        before: 0.0,
        action: 0,
        templates: 0,
        background: 0,
        sum: 0.0,
        basis: 0.0,
        wander: 0.0,
        jitter: 0.0,
    };
}
struct Window {
    token: u32,
    tag: u32,
    numerical: u32,
    cols: usize,
    rows: usize,
    rows0: usize,
    status: i32,
    log: bool,
    data: [Row; ROWS],
    matrix: [f32; ROWS * COLS],
    decision: Decision,
    enqueued_us: u64,
    computed_us: u64,
}
impl Window {
    const ZERO: Self = Self {
        token: 0,
        tag: 0,
        numerical: 0,
        cols: 0,
        rows: 0,
        rows0: 0,
        status: 0,
        log: false,
        data: [Row::ZERO; ROWS],
        matrix: [0.0; ROWS * COLS],
        decision: Decision::ZERO,
        enqueued_us: 0,
        computed_us: 0,
    };
    fn copy_from(&mut self, other: &Self) {
        self.token = other.token;
        self.tag = other.tag;
        self.numerical = other.numerical;
        self.cols = other.cols;
        self.rows = other.rows;
        self.rows0 = other.rows0;
        self.status = other.status;
        self.log = other.log;
        self.decision = other.decision;
        self.enqueued_us = other.enqueued_us;
        self.computed_us = other.computed_us;
        self.data[..self.rows].copy_from_slice(&other.data[..other.rows]);
        self.matrix[..self.rows * self.cols]
            .copy_from_slice(&other.matrix[..other.rows * self.cols]);
    }
    fn write(&self, out: &mut impl Write) -> io::Result<()> {
        let d = self.decision;
        writeln!(out,"PRESENCE_TRACE v=1 tag={} token={} window={} cols={} rows={} rows0={} status={} log={} enqueued_us={} computed_us={} decision_status={} train={} before_basis={:08x} action={} templates={} background={} sum={:08x} basis={:08x} wander={:08x} jitter={:08x}",self.tag,self.token,self.numerical,self.cols,self.rows,self.rows0,self.status,u8::from(self.log),self.enqueued_us,self.computed_us,d.status,d.train,d.before.to_bits(),d.action,d.templates,d.background,d.sum.to_bits(),d.basis.to_bits(),d.wander.to_bits(),d.jitter.to_bits())?;
        let mut hash = 2166136261;
        for word in [
            1,
            self.tag,
            self.token,
            self.numerical,
            self.cols as u32,
            self.rows as u32,
            self.rows0 as u32,
            self.status as u32,
            self.log as u32,
            self.enqueued_us as u32,
            (self.enqueued_us >> 32) as u32,
            self.computed_us as u32,
            (self.computed_us >> 32) as u32,
            d.status as u32,
            d.train,
            d.before.to_bits(),
            d.action,
            d.templates,
            d.background,
            d.sum.to_bits(),
            d.basis.to_bits(),
            d.wander.to_bits(),
            d.jitter.to_bits(),
        ] {
            hash_word(&mut hash, word);
        }
        for (index, row) in self.data[..self.rows].iter().enumerate() {
            let mut words = [0u32; META + 3 * COLS];
            for (target, word) in words.iter_mut().zip(
                row.meta
                    .into_iter()
                    .chain(row.iq[..2 * self.cols].iter().map(|v| *v as i32 as u32))
                    .chain(row.amplitude[..self.cols].iter().map(|v| v.to_bits())),
            ) {
                *target = word;
            }
            for (chunk_index, chunk) in words[..META + 3 * self.cols].chunks(16).enumerate() {
                write!(
                    out,
                    "PRESENCE_TRACE_WORDS row={index} index={} bits=",
                    chunk_index * 16
                )?;
                for (i, &word) in chunk.iter().enumerate() {
                    if i != 0 {
                        write!(out, ",")?;
                    }
                    write!(out, "{word:08x}")?;
                    hash_word(&mut hash, word);
                }
                writeln!(out)?;
            }
        }
        writeln!(
            out,
            "PRESENCE_TRACE_END token={} fnv={hash:08x}",
            self.token
        )
    }
}
fn hash_word(hash: &mut u32, word: u32) {
    for byte in word.to_le_bytes() {
        *hash = (*hash ^ byte as u32).wrapping_mul(16777619);
    }
}
struct Slot {
    token: AtomicU32,
    value: Mutex<Window>,
}
impl Slot {
    const fn new() -> Self {
        Self {
            token: AtomicU32::new(0),
            value: Mutex::new(Window::ZERO),
        }
    }
}
pub struct Trace {
    pub enabled: AtomicBool,
    source_mac: OnceLock<[u8; 6]>,
    gain_address: AtomicUsize,
    source_active: AtomicBool,
    pub other_peer_frames: AtomicU32,
    rf: Mutex<[Rf; 32]>,
    rows: Mutex<RowStore>,
    slots: [Slot; QUEUED],
    exported: Mutex<Window>,
    ready: AtomicBool,
    pending: AtomicU32,
    active_tag: AtomicU32,
    computing: AtomicBool,
    owner: AtomicUsize,
    last_tag: AtomicU32,
    next: AtomicU32,
    gain_sequence: AtomicU32,
    first_failure: AtomicBool,
    pub faults: AtomicU32,
    pub first_fault_line: AtomicU32,
    pub rf_evictions: AtomicU32,
    pub gain_unavailable_rows: AtomicU32,
    pub gain_ready: AtomicBool,
}
impl Trace {
    pub const fn new() -> Self {
        Self {
            enabled: AtomicBool::new(false),
            source_mac: OnceLock::new(),
            gain_address: AtomicUsize::new(0),
            source_active: AtomicBool::new(false),
            other_peer_frames: AtomicU32::new(0),
            rf: Mutex::new([Rf::ZERO; 32]),
            rows: Mutex::new(RowStore {
                peer: 0,
                rows: [Row::ZERO; RING],
            }),
            slots: [const { Slot::new() }; QUEUED],
            exported: Mutex::new(Window::ZERO),
            ready: AtomicBool::new(false),
            pending: AtomicU32::new(0),
            active_tag: AtomicU32::new(0),
            computing: AtomicBool::new(false),
            owner: AtomicUsize::new(0),
            last_tag: AtomicU32::new(0),
            next: AtomicU32::new(0),
            gain_sequence: AtomicU32::new(0),
            first_failure: AtomicBool::new(true),
            faults: AtomicU32::new(0),
            first_fault_line: AtomicU32::new(0),
            rf_evictions: AtomicU32::new(0),
            gain_unavailable_rows: AtomicU32::new(0),
            gain_ready: AtomicBool::new(false),
        }
    }
    #[track_caller]
    fn fault(&self) {
        crate::calibration::RUNTIME.fail(crate::calibration::Rejection::TraceFault);
        let _ = self.first_fault_line.compare_exchange(
            0,
            std::panic::Location::caller().line(),
            Ordering::AcqRel,
            Ordering::Relaxed,
        );
        self.faults.fetch_add(1, Ordering::Relaxed);
    }
    #[track_caller]
    fn reject(&self) -> bool {
        self.fault();
        false
    }

    pub fn select_source(&self, mac: [u8; 6]) -> bool {
        !self.enabled.load(Ordering::Acquire)
            && mac != [0; 6]
            && mac != [255; 6]
            && self.source_mac.set(mac).is_ok()
    }
    pub fn matches(&self, mac: &[u8; 6]) -> bool {
        self.source_mac.get() == Some(mac)
    }
    pub fn other_frame(&self) {
        self.gain_address.store(0, Ordering::Release);
        self.other_peer_frames.fetch_add(1, Ordering::Relaxed);
    }
    pub fn gain_admitted(&self, address: usize) -> bool {
        address != 0 && self.gain_address.load(Ordering::Acquire) == address
    }
    pub fn capture_admitted(&self) -> bool {
        !self.enabled.load(Ordering::Acquire) || self.source_active.load(Ordering::Acquire)
    }
    /// SDK detection calls run on the claimed numerical worker. An active
    /// selected-source scope from another task is never borrowed for attribution.
    pub fn numerical_source(&self, task: usize) -> Option<crate::failures::Source> {
        if !self.enabled.load(Ordering::Acquire) || task == 0 {
            return None;
        }
        let owner = self.owner.load(Ordering::Acquire);
        if owner != 0 && owner != task {
            return None;
        }
        Some(if self.source_active.load(Ordering::Acquire) {
            crate::failures::Source::Selected
        } else {
            crate::failures::Source::Other
        })
    }
    pub fn prepare(&self) -> bool {
        !self.enabled.load(Ordering::Acquire)
            && self.source_mac.get().is_some()
            && self.rf.try_lock().is_ok()
            && self.rows.try_lock().is_ok()
            && self.exported.try_lock().is_ok()
            && self.slots.iter().all(|s| s.value.try_lock().is_ok())
    }
    pub fn current_tag(&self) -> u32 {
        self.active_tag.load(Ordering::Acquire)
    }
    fn claim(&self, task: usize) -> bool {
        let owner = self.owner.load(Ordering::Acquire);
        if task != 0
            && (owner == task
                || (owner == 0
                    && self
                        .owner
                        .compare_exchange(0, task, Ordering::AcqRel, Ordering::Relaxed)
                        .is_ok()))
        {
            true
        } else {
            self.fault();
            false
        }
    }
    pub fn arm(&self, tag: u32) -> bool {
        if !self.enabled.load(Ordering::Acquire)
            || tag == 0
            || tag > 16
            || tag <= self.last_tag.load(Ordering::Relaxed)
            || self.ready.load(Ordering::Acquire)
        {
            return false;
        }
        if self
            .pending
            .compare_exchange(0, tag, Ordering::AcqRel, Ordering::Relaxed)
            .is_err()
        {
            return false;
        }
        self.last_tag.store(tag, Ordering::Relaxed);
        true
    }
    pub fn rf(&self, address: usize, radio: Radio) {
        if !self.enabled.load(Ordering::Acquire) {
            return;
        }
        self.gain_address.store(address, Ordering::Release);
        if address == 0
            || radio.length == 0
            || radio.length > 512
            || radio.data_type > 1
            || radio.agc > 255
            || !(-128..=127).contains(&radio.fft)
        {
            self.fault();
            return;
        }
        let Ok(mut records) = self.rf.try_lock() else {
            self.fault();
            return;
        };
        for old in records.iter_mut() {
            if old.address == address {
                old.address = 0;
                self.rf_evictions.fetch_add(1, Ordering::Relaxed);
            }
        }
        let index = radio.sequence as usize % records.len();
        if records[index].address != 0 {
            self.rf_evictions.fetch_add(1, Ordering::Relaxed);
        }
        records[index] = Rf {
            address,
            radio,
            ..Rf::ZERO
        };
    }
    pub fn compensation(
        &self,
        address: usize,
        agc: u8,
        fft: i8,
        gain_state: i32,
        status: i32,
        factor: f32,
        before: Summary,
        after: Summary,
    ) {
        if !self.enabled.load(Ordering::Acquire) {
            return;
        }
        let Ok(mut records) = self.rf.try_lock() else {
            self.fault();
            return;
        };
        let mut matches = records
            .iter_mut()
            .filter(|r| r.address == address && address != 0);
        let Some(r) = matches.next() else {
            self.fault();
            return;
        };
        if matches.next().is_some()
            || r.gain_sequence != 0
            || r.radio.agc != agc as u32
            || r.radio.fft != fft as i32
            || before.bytes != r.radio.length
            || after.bytes != before.bytes
        {
            self.fault();
            return;
        }
        let sequence = self
            .gain_sequence
            .fetch_add(1, Ordering::Relaxed)
            .wrapping_add(1);
        if sequence == 0 {
            self.fault();
            return;
        }
        r.gain_sequence = sequence;
        r.gain_state = gain_state;
        r.status = status;
        r.factor = factor;
        r.before = before;
        r.after = after;
    }
    pub fn row(
        &self,
        peer: usize,
        index: usize,
        address: usize,
        radio: Radio,
        iq: &[i16],
        amplitude: &[f32],
        log: bool,
        step: u32,
        post: Summary,
        processed_us: u64,
    ) {
        if !self.enabled.load(Ordering::Acquire) {
            return;
        }
        let cols = amplitude.len();
        if peer == 0
            || index >= RING
            || cols == 0
            || cols > COLS
            || step == 0
            || step > 255
            || iq.len() != cols * 2
        {
            self.fault();
            return;
        }
        let Ok(mut records) = self.rf.try_lock() else {
            self.fault();
            return;
        };
        let rf_index = radio.sequence as usize % records.len();
        let entry = &mut records[rf_index];
        if entry.address != address
            || entry.radio.sequence != radio.sequence
            || entry.radio.timestamp != radio.timestamp
            || entry.radio.rssi != radio.rssi
            || entry.radio.agc != radio.agc
            || entry.radio.fft != radio.fft
            || entry.radio.length != radio.length
            || entry.radio.data_type != radio.data_type
        {
            self.fault();
            return;
        }
        if entry.gain_sequence == 0 {
            self.fault();
            return;
        }
        if post.hash != entry.after.hash
            || post.bytes != entry.after.bytes
            || post.zeros != entry.after.zeros
        {
            self.fault();
            return;
        }
        let available = entry.status == 0
            && entry.factor.is_finite()
            && entry.factor > 0.0
            && entry.factor.to_bits() == radio.factor.to_bits();
        if !available {
            // Only the documented startup baseline status may pass as unknown.
            // No failed output is read or invented; qualified samples still need
            // a complete available window. A later loss is a sticky failure.
            self.gain_unavailable_rows.fetch_add(1, Ordering::Relaxed);
            if entry.status != 0x103
                || entry.gain_state != 0
                || self.gain_ready.load(Ordering::Acquire)
            {
                self.fault();
                return;
            }
        }
        let Ok(mut rows) = self.rows.try_lock() else {
            self.fault();
            return;
        };
        if rows.peer != 0 && rows.peer != peer {
            self.fault();
            return;
        }
        rows.peer = peer;
        let row = &mut rows.rows[index];
        row.valid = true;
        row.cols = cols;
        row.meta = [
            radio.sequence,
            radio.timestamp,
            radio.rssi as u32,
            radio.agc,
            radio.fft as u32,
            radio.data_type,
            radio.length,
            entry.gain_sequence,
            entry.status as u32,
            entry.factor.to_bits(),
            entry.before.hash,
            entry.after.hash,
            entry.before.zeros,
            entry.after.zeros,
            entry.before.bytes,
            log as u32,
            available as u32,
            step,
            entry.radio.observed_lo,
            entry.radio.observed_hi,
            processed_us as u32,
            (processed_us >> 32) as u32,
        ];
        row.iq[..iq.len()].copy_from_slice(iq);
        row.amplitude[..cols].copy_from_slice(amplitude);
        entry.address = 0;
    }
    pub fn enqueue(
        &self,
        peer: usize,
        begin: usize,
        count: usize,
        ring: usize,
        cols: usize,
        expected: &[u32],
        timestamps: &[u32],
        us: u64,
    ) -> u32 {
        if !self.enabled.load(Ordering::Acquire) {
            return 0;
        }
        if ring == 0
            || ring > RING
            || begin >= ring
            || count == 0
            || count > ROWS
            || count > ring
            || expected.len() != ring
            || timestamps.len() != ring
            || cols == 0
            || cols > COLS
        {
            self.fault();
            return 0;
        }
        let token = self.next.fetch_add(1, Ordering::Relaxed).wrapping_add(1);
        if token == 0 {
            self.fault();
            return 0;
        }
        let Some(slot) = self.slots.iter().find(|s| {
            s.token
                .compare_exchange(0, token, Ordering::AcqRel, Ordering::Relaxed)
                .is_ok()
        }) else {
            self.fault();
            return 0;
        };
        let result = (|| {
            let Ok(rows) = self.rows.try_lock() else {
                return self.reject();
            };
            let Ok(mut w) = slot.value.try_lock() else {
                return self.reject();
            };
            if rows.peer != peer {
                return self.reject();
            }
            w.token = token;
            w.tag = 0;
            w.numerical = 0;
            w.cols = cols;
            w.rows = count;
            w.rows0 = count.min(ring - begin);
            w.status = 0;
            w.decision = Decision::ZERO;
            w.enqueued_us = us;
            w.computed_us = 0;
            for i in 0..count {
                let row = &rows.rows[(begin + i) % ring];
                if !row.valid
                    || row.cols != cols
                    || row.meta[0] != expected[(begin + i) % ring]
                    || row.meta[1] / 1000 != timestamps[(begin + i) % ring]
                {
                    return self.reject();
                }
                if i > 0 && row.meta[0] <= w.data[i - 1].meta[0] {
                    return self.reject();
                }
                if i > 0 {
                    let delta = row.meta[1].wrapping_sub(w.data[i - 1].meta[1]);
                    if delta == 0 || delta >= 0x80000000 {
                        return self.reject();
                    }
                }
                w.data[i] = *row;
                w.matrix[i * cols..(i + 1) * cols].copy_from_slice(&row.amplitude[..cols]);
                if i > 0 && row.meta[15] != w.log as u32 {
                    return self.reject();
                }
                w.log = row.meta[15] != 0;
            }
            if w.data[..count].iter().all(|r| r.meta[16] == 1) {
                self.gain_ready.store(true, Ordering::Release);
            }
            true
        })();
        if !result {
            slot.token.store(0, Ordering::Release);
            self.fault();
            return 0;
        }
        token
    }
    pub fn compute(
        &self,
        token: u32,
        cols: usize,
        rows0: usize,
        rows1: usize,
        us: u64,
        call: impl FnOnce(*const f32, *const f32) -> (i32, u32),
    ) -> i32 {
        let Some(slot) = self
            .slots
            .iter()
            .find(|s| token != 0 && s.token.load(Ordering::Acquire) == token)
        else {
            self.fault();
            return 0x103;
        };
        let Ok(mut w) = slot.value.try_lock() else {
            self.fault();
            return 0x103;
        };
        if w.numerical != 0
            || w.cols != cols
            || w.rows0 != rows0
            || rows0.checked_add(rows1) != Some(w.rows)
        {
            self.fault();
            return 0x103;
        }
        if self
            .computing
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Relaxed)
            .is_err()
        {
            self.fault();
            return 0x103;
        }
        w.tag = if w.data[..w.rows].iter().all(|r| r.meta[16] == 1) {
            self.pending.load(Ordering::Acquire)
        } else {
            0
        };
        self.active_tag.store(w.tag, Ordering::Release);
        self.source_active.store(true, Ordering::Release);
        w.computed_us = us;
        let (status, numerical) = call(w.matrix.as_ptr(), unsafe {
            w.matrix.as_ptr().add(rows0 * cols)
        });
        self.active_tag.store(0, Ordering::Release);
        self.computing.store(false, Ordering::Release);
        w.status = status;
        w.numerical = numerical;
        if numerical == 0 {
            self.fault();
        }
        if status != 0 {
            crate::calibration::RUNTIME.fail(crate::calibration::Rejection::NativeError);
            self.source_active.store(false, Ordering::Release);
            self.publish(&w);
            drop(w);
            slot.token.store(0, Ordering::Release);
        }
        status
    }
    fn publish(&self, w: &Window) {
        let failure = w.status != 0 && self.first_failure.load(Ordering::Relaxed);
        if !failure && (w.tag == 0 || self.pending.load(Ordering::Acquire) != w.tag) {
            return;
        }
        if self.ready.load(Ordering::Acquire) {
            self.fault();
            return;
        }
        let Ok(mut output) = self.exported.try_lock() else {
            self.fault();
            return;
        };
        output.copy_from(w);
        self.ready.store(true, Ordering::Release);
        if failure {
            self.first_failure.store(false, Ordering::Relaxed);
        } else {
            self.pending.store(0, Ordering::Release);
        }
    }
    pub fn basis(&self, token: u32, value: f32) {
        if !self.enabled.load(Ordering::Acquire) {
            return;
        }
        if let Some(slot) = self
            .slots
            .iter()
            .find(|s| token != 0 && s.token.load(Ordering::Acquire) == token)
        {
            if let Ok(mut w) = slot.value.try_lock() {
                w.decision.before = value;
                return;
            }
        }
        self.fault();
    }
    pub fn decision(
        &self,
        token: u32,
        status: i32,
        train: u32,
        action: u32,
        templates: u32,
        background: u32,
        sum: f32,
        basis: f32,
        wander: f32,
        jitter: f32,
    ) {
        if !self.enabled.load(Ordering::Acquire) {
            return;
        }
        let Some(slot) = self
            .slots
            .iter()
            .find(|s| token != 0 && s.token.load(Ordering::Acquire) == token)
        else {
            self.fault();
            return;
        };
        let Ok(mut w) = slot.value.try_lock() else {
            self.fault();
            return;
        };
        if w.numerical == 0 {
            self.fault();
            return;
        }
        self.source_active.store(false, Ordering::Release);
        w.decision = Decision {
            status,
            train,
            before: w.decision.before,
            action,
            templates,
            background,
            sum,
            basis,
            wander,
            jitter,
        };
        #[cfg(target_os = "espidf")]
        {
            self.observe_decision(
                &mut w,
                crate::calibration::now_us,
                &crate::calibration::RUNTIME,
                &crate::diagnostics::DIAGNOSTICS,
                &crate::capture::RECORDER,
            );
        }
        self.publish(&w);
        drop(w);
        slot.token.store(0, Ordering::Release);
    }
    pub fn dump(&self, out: &mut impl Write) -> io::Result<()> {
        if !self.ready.load(Ordering::Acquire) {
            return Ok(());
        }
        let Ok(w) = self.exported.try_lock() else {
            return Ok(());
        };
        w.write(out)?;
        self.ready.store(false, Ordering::Release);
        Ok(())
    }
    fn retain_decision(
        &self,
        w: &mut Window,
        tag: u32,
        recorder: &crate::capture::Recorder,
    ) -> bool {
        if !(14..=16).contains(&tag)
            || w.status != 0
            || tag <= self.last_tag.load(Ordering::Acquire)
            || self.ready.load(Ordering::Acquire)
            || self
                .pending
                .compare_exchange(0, tag, Ordering::AcqRel, Ordering::Relaxed)
                .is_err()
        {
            self.fault();
            return false;
        }
        if !recorder.retain_current(w.numerical, tag) {
            self.pending.store(0, Ordering::Release);
            self.fault();
            return false;
        }
        w.tag = tag;
        self.last_tag.store(tag, Ordering::Release);
        self.publish(w);
        self.ready.load(Ordering::Acquire) && self.pending.load(Ordering::Acquire) == 0
    }
    fn observe_decision(
        &self,
        w: &mut Window,
        clock: impl FnOnce() -> u64,
        runtime: &crate::calibration::Runtime,
        diagnostics: &crate::diagnostics::Diagnostics,
        recorder: &crate::capture::Recorder,
    ) {
        let first = &w.data[0].meta;
        let last = &w.data[w.rows - 1].meta;
        let sequence = last[0];
        let window = w.numerical;
        let complete = w.decision.status == 0 && w.decision.train == 2;
        let feature = crate::calibration::Feature {
            sequence,
            oldest_us: u64::from(first[18]) | (u64::from(first[19]) << 32),
            newest_us: u64::from(last[18]) | (u64::from(last[19]) << 32),
            templates: w.decision.templates,
            training_complete: complete,
            gain_available: w.data[..w.rows].iter().all(|row| row.meta[16] == 1),
            wander: w.decision.wander,
        };
        let after = |value: &crate::calibration::Calibration, now| {
            diagnostics.capture(window, sequence, complete, value.diagnostic(now), |tag| {
                self.retain_decision(w, tag, recorder)
            })
        };
        if crate::lab::LAB.enabled() {
            runtime.execute_with(
                csi_lab_core::journal::Operation::Observe(feature),
                clock,
                |mut r| {
                    r.words[23] = window;
                    crate::lab::model(r);
                },
                after,
            );
        } else {
            runtime.observe_with(feature, clock, after);
        }
    }
}
pub static TRACE: Trace = Trace::new();
#[cfg(target_os = "espidf")]
pub unsafe fn byte_summary(data: *const u8, len: usize) -> Summary {
    if !TRACE.enabled.load(Ordering::Acquire) || data.is_null() || len == 0 || len > 512 {
        return Summary::default();
    }
    summarize(std::slice::from_raw_parts(data, len))
}
fn summarize(data: &[u8]) -> Summary {
    let mut hash = 2166136261;
    for &b in data {
        hash = (hash ^ b as u32).wrapping_mul(16777619);
    }
    Summary {
        hash,
        zeros: data.iter().filter(|&&b| b == 0).count() as u32,
        bytes: data.len() as u32,
    }
}
/// Mirrors the pinned packed-12-bit extraction, rather than assuming int16 CSI.
fn selected_iq(data: &[u8], cols: usize, step: usize, wide: bool) -> Option<[i16; COLS * 2]> {
    let width = if wide { 4 } else { 2 };
    if cols == 0
        || cols > COLS
        || step == 0
        || (cols - 1)
            .checked_mul(step)?
            .checked_mul(width)?
            .checked_add(width)?
            > data.len()
    {
        return None;
    }
    let mut out = [0; COLS * 2];
    for i in 0..cols {
        let offset = i * step * width;
        for j in 0..2 {
            out[2 * i + j] = if wide {
                let at = offset + 2 * j;
                let hi = (data[at + 1] as i16).wrapping_shl(12) >> 4;
                ((hi | data[at] as i16) >> 4) as i16
            } else {
                data[offset + j] as i8 as i16
            };
        }
    }
    Some(out)
}
#[cfg(target_os = "espidf")]
mod native {
    use super::*;
    #[no_mangle]
    pub extern "C" fn presence_trace_active() -> bool {
        TRACE.enabled.load(Ordering::Acquire)
    }
    #[no_mangle]
    pub unsafe extern "C" fn presence_trace_matches(mac: *const u8) -> bool {
        if mac.is_null() {
            TRACE.fault();
            return false;
        }
        TRACE.matches(&*(mac as *const [u8; 6]))
    }
    #[no_mangle]
    pub extern "C" fn presence_trace_other_rf() {
        TRACE.other_frame();
    }
    #[no_mangle]
    pub unsafe extern "C" fn presence_trace_rf(data: *const u8, radio: *const Radio) {
        if let Some(r) = radio.as_ref() {
            TRACE.rf(data as usize, *r);
        }
    }
    #[no_mangle]
    pub unsafe extern "C" fn presence_trace_row(
        peer: usize,
        index: u32,
        address: *const u8,
        radio: *const Radio,
        ltf: *const u8,
        len: u32,
        output: *const f32,
        cols: u32,
        step: u32,
        log: bool,
    ) {
        if !TRACE.enabled.load(Ordering::Acquire) {
            return;
        }
        let Some(r) = radio.as_ref() else {
            TRACE.fault();
            return;
        };
        if ltf.is_null() || output.is_null() || len > 512 || cols == 0 || cols as usize > COLS {
            TRACE.fault();
            return;
        }
        let Some(iq) = selected_iq(
            std::slice::from_raw_parts(ltf, len as usize),
            cols as usize,
            step as usize,
            r.data_type == 1,
        ) else {
            TRACE.fault();
            return;
        };
        TRACE.row(
            peer,
            index as usize,
            address as usize,
            *r,
            &iq[..2 * cols as usize],
            std::slice::from_raw_parts(output, cols as usize),
            log,
            step,
            byte_summary(address, r.length as usize),
            esp_timer_get_time() as u64,
        );
    }
    #[no_mangle]
    pub unsafe extern "C" fn presence_trace_enqueue(
        peer: usize,
        begin: u32,
        count: u32,
        ring: u32,
        cols: u32,
        expected: *const u32,
        timestamps: *const u32,
    ) -> u32 {
        if ring == 0 || ring as usize > RING || expected.is_null() || timestamps.is_null() {
            TRACE.fault();
            return 0;
        }
        TRACE.enqueue(
            peer,
            begin as usize,
            count as usize,
            ring as usize,
            cols as usize,
            std::slice::from_raw_parts(expected, ring as usize),
            std::slice::from_raw_parts(timestamps, ring as usize),
            esp_timer_get_time() as u64,
        )
    }
    extern "C" {
        fn esp_timer_get_time() -> i64;
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
    pub unsafe extern "C" fn presence_trace_compute(
        token: u32,
        cols: u32,
        rows0: u32,
        rows1: u32,
        output: *mut f32,
    ) -> i32 {
        if !TRACE.claim(presence_task_identity() as usize) {
            return 0x103;
        }
        TRACE.compute(
            token,
            cols as usize,
            rows0 as usize,
            rows1 as usize,
            esp_timer_get_time() as u64,
            |a, b| {
                let status = esp_radar_motion_dec_compute(cols, rows0, a, rows1, b, output);
                (status, crate::numerical::window_count())
            },
        )
    }
    #[no_mangle]
    pub extern "C" fn presence_trace_basis(token: u32, value: f32) {
        TRACE.basis(token, value);
    }
    #[no_mangle]
    pub extern "C" fn presence_trace_decision(
        token: u32,
        status: i32,
        train: u32,
        action: u32,
        templates: u32,
        background: u32,
        sum: f32,
        basis: f32,
        wander: f32,
        jitter: f32,
    ) {
        TRACE.decision(
            token, status, train, action, templates, background, sum, basis, wander, jitter,
        );
    }
    #[no_mangle]
    pub static presence_trace_static_bytes: u32 = std::mem::size_of::<Trace>() as u32;
    #[no_mangle]
    pub static presence_trace_rust_abi: [u32; 14] = [
        std::mem::size_of::<Radio>() as u32,
        std::mem::align_of::<Radio>() as u32,
        std::mem::offset_of!(Radio, sequence) as u32,
        std::mem::offset_of!(Radio, timestamp) as u32,
        std::mem::offset_of!(Radio, rssi) as u32,
        std::mem::offset_of!(Radio, agc) as u32,
        std::mem::offset_of!(Radio, fft) as u32,
        std::mem::offset_of!(Radio, data_type) as u32,
        std::mem::offset_of!(Radio, length) as u32,
        std::mem::offset_of!(Radio, factor) as u32,
        std::mem::offset_of!(Radio, observed_lo) as u32,
        std::mem::offset_of!(Radio, observed_hi) as u32,
        0,
        1,
    ];
}

#[cfg(test)]
mod tests {
    #[test]
    fn failure_scope_is_worker_bound_and_never_borrows_another_tasks_active_source() {
        use crate::failures::Source;
        let t = Trace::new();
        assert_eq!(t.numerical_source(7), None);
        t.enabled.store(true, Ordering::Release);
        assert_eq!(t.numerical_source(0), None);
        assert_eq!(t.numerical_source(7), Some(Source::Other));
        assert!(t.claim(7));
        t.source_active.store(true, Ordering::Release);
        assert_eq!(t.numerical_source(7), Some(Source::Selected));
        assert_eq!(t.numerical_source(8), None);
        t.source_active.store(false, Ordering::Release);
        assert_eq!(t.numerical_source(7), Some(Source::Other));
    }

    use super::*;
    fn enabled() -> Trace {
        let trace = Trace::new();
        assert!(trace.select_source([1, 2, 3, 4, 5, 6]));
        assert!(trace.prepare());
        trace.enabled.store(true, Ordering::Release);
        trace
    }
    fn frame(t: &Trace, seq: u32, index: usize, amplitude: &[f32]) {
        let radio = Radio {
            sequence: seq,
            timestamp: seq.wrapping_mul(1000),
            rssi: -45,
            agc: 32,
            fft: -7,
            data_type: 0,
            length: 4,
            factor: 0.5,
            observed_lo: seq * 1000,
            observed_hi: 0,
        };
        let bytes = [3, 4, 0, 5];
        let summary = summarize(&bytes);
        let address = 100 + seq as usize;
        t.rf(address, radio);
        t.compensation(address, 32, -7, 1, 0, 0.5, summary, summary);
        t.row(
            1,
            index,
            address,
            radio,
            &[3, 4, 0, 5],
            amplitude,
            false,
            1,
            summary,
            seq as u64 * 1000 + 100,
        );
    }
    fn enqueue(t: &Trace) -> u32 {
        t.enqueue(1, 0, 2, 2, 2, &[1, 2], &[1, 2], 3000)
    }
    fn ready(t: &Trace) -> u32 {
        frame(t, 1, 0, &[5.0, 5.0]);
        frame(t, 2, 1, &[5.0, 5.0]);
        enqueue(t)
    }
    fn decision(t: &Trace, token: u32) {
        t.basis(token, 0.001);
        t.decision(token, 0, 1, 4, 10, 100, 0.1, 0.002, 0.01, 0.02);
    }
    #[test]
    fn automatic_decision_pair_keeps_exact_current_work_after_same_task_wander_calls() {
        // Numerical functions are stubbed here: this qualifies identity and
        // storage transfer, not the correctness of the numerical algorithm.
        let t = enabled();
        let token = ready(&t);
        let r = crate::capture::Recorder::new();
        assert!(r.prepare());
        r.enabled.store(true, Ordering::Release);
        let mut output = [0.0f32; 2];
        assert_eq!(
            t.compute(token, 2, 2, 0, 4000, |a, _| {
                let values = unsafe { std::slice::from_raw_parts(a, 4) };
                let status = unsafe {
                    r.motion(
                        1,
                        7,
                        4000,
                        2,
                        2,
                        0,
                        values,
                        &[],
                        output.as_mut_ptr(),
                        |_, _| {
                            output = [1.0, 2.0];
                            0
                        },
                    )
                };
                assert_eq!(r.wander(1, &[2.0, 4.0], &output, |_, _| 0.125), 0.125);
                (status, 7)
            }),
            0
        );
        let runtime = crate::calibration::Runtime::new();
        assert!(runtime.prepare(crate::calibration::Config::EMPTY_HOLDOUT));
        let d = crate::diagnostics::Diagnostics::new();
        assert!(d.prepare());
        let slot = t
            .slots
            .iter()
            .find(|s| s.token.load(Ordering::Acquire) == token)
            .unwrap();
        {
            let mut w = slot.value.lock().unwrap();
            w.decision = Decision {
                status: 0,
                train: 2,
                templates: 10,
                wander: 0.125,
                ..Decision::ZERO
            };
            t.observe_decision(&mut w, || 5000, &runtime, &d, &r);
        }
        let mut numerical = Vec::new();
        r.dump(&mut numerical).unwrap();
        let numerical = String::from_utf8(numerical).unwrap();
        assert!(numerical.contains("kind=sample tag=14 window=7"));
        assert!(numerical.contains("part=matrix index=0 bits=40a00000,40a00000,40a00000,40a00000"));
        let mut trace = Vec::new();
        t.dump(&mut trace).unwrap();
        assert!(String::from_utf8(trace)
            .unwrap()
            .contains("tag=14 token=1 window=7"));
        let mut meta = Vec::new();
        d.dump(&mut meta).unwrap();
        assert!(String::from_utf8(meta)
            .unwrap()
            .contains("tag=14 window=7 sequence=2"));
        assert_eq!(t.faults.load(Ordering::Acquire), 0);
        assert_eq!(d.failures.load(Ordering::Acquire), 0);
        assert!(!r.retain_current(8, 15));
    }
    #[test]
    fn queued_inputs_keep_source_identity_after_ring_replacement_and_export_exact_fixture() {
        let t = enabled();
        assert!(t.arm(1));
        let token = ready(&t);
        assert_ne!(token, 0);
        frame(&t, 3, 0, &[50.0, 50.0]);
        frame(&t, 4, 1, &[60.0, 60.0]);
        assert_eq!(
            t.compute(token, 2, 2, 0, 4000, |a, _| {
                assert_eq!(unsafe { std::slice::from_raw_parts(a, 4) }, &[5.0; 4]);
                assert_eq!(t.current_tag(), 1);
                assert!(t.capture_admitted());
                (0, 7)
            }),
            0
        );
        assert_eq!(t.current_tag(), 0);
        assert!(t.capture_admitted());
        decision(&t, token);
        assert!(!t.capture_admitted());
        let mut out = Vec::new();
        t.dump(&mut out).unwrap();
        assert_eq!(
            String::from_utf8(out).unwrap(),
            include_str!("../fixtures/trace-v1.txt")
        );
        assert_eq!(t.faults.load(Ordering::Relaxed), 0);
    }
    #[test]
    fn unknown_gain_mismatched_payload_missing_rows_and_foreign_peer_are_rejected() {
        let t = enabled();
        let mut radio = Radio {
            sequence: 1,
            timestamp: 1000,
            rssi: -45,
            agc: 32,
            fft: -7,
            data_type: 0,
            length: 4,
            factor: 0.5,
            observed_lo: 1000,
            observed_hi: 0,
        };
        let s = summarize(&[3, 4, 0, 5]);
        t.rf(1, radio);
        t.row(
            1,
            0,
            1,
            radio,
            &[3, 4, 0, 5],
            &[5.0, 5.0],
            false,
            1,
            s,
            2100,
        );
        assert_ne!(t.faults.load(Ordering::Relaxed), 0);
        assert_eq!(t.enqueue(1, 0, 1, 2, 2, &[1, 0], &[1, 0], 3000), 0);
        t.compensation(1, 32, -7, 1, 0, 0.5, s, s);
        radio.factor = 0.25;
        t.row(
            1,
            0,
            1,
            radio,
            &[3, 4, 0, 5],
            &[5.0, 5.0],
            false,
            1,
            s,
            2100,
        );
        assert_eq!(t.enqueue(1, 0, 1, 2, 2, &[1, 0], &[1, 0], 3000), 0);
        let t = enabled();
        ready(&t);
        assert_eq!(t.enqueue(2, 0, 2, 2, 2, &[1, 2], &[1, 2], 3000), 0);
        assert_eq!(t.enqueue(1, 0, 2, 2, 2, &[8, 2], &[1, 2], 3000), 0);
        assert_eq!(t.enqueue(1, 0, 2, 2, 2, &[1, 2], &[8, 2], 3000), 0);
    }
    #[test]
    fn paused_consumers_contention_and_queue_overload_never_wait() {
        let t = enabled();
        let paused = t.rf.lock().unwrap();
        t.rf(
            1,
            Radio {
                length: 4,
                ..Radio::default()
            },
        );
        drop(paused);
        assert_eq!(t.faults.load(Ordering::Relaxed), 1);
        let first = t.first_fault_line.load(Ordering::Acquire);
        assert_ne!(first, 0);
        t.fault();
        assert_eq!(t.first_fault_line.load(Ordering::Acquire), first);
        ready(&t);
        let paused = t.rows.lock().unwrap();
        assert_eq!(enqueue(&t), 0);
        drop(paused);
        // One slot is already queued. Exhaust exactly the remaining eight.
        for _ in 1..QUEUED {
            assert_ne!(enqueue(&t), 0);
        }
        assert_eq!(enqueue(&t), 0);
        let token = t.slots[0].token.load(Ordering::Acquire);
        let paused = t.slots[0].value.lock().unwrap();
        assert_eq!(
            t.compute(token, 2, 2, 0, 4000, |_, _| panic!(
                "must not call native without owned input"
            )),
            0x103
        );
        drop(paused);
    }
    #[test]
    fn duplicate_compute_shape_mismatch_and_missing_token_do_not_substitute_native_output() {
        let t = enabled();
        let token = ready(&t);
        assert_eq!(t.compute(token, 2, 1, 1, 4000, |_, _| panic!()), 0x103);
        assert_eq!(t.compute(999, 2, 2, 0, 4000, |_, _| panic!()), 0x103);
        assert_eq!(t.compute(token, 2, 2, 0, 4000, |_, _| (0, 1)), 0);
        assert_eq!(t.compute(token, 2, 2, 0, 4000, |_, _| panic!()), 0x103);
        decision(&t, token);
        assert_eq!(t.compute(token, 2, 2, 0, 4000, |_, _| panic!()), 0x103);
    }
    #[test]
    fn failed_native_output_is_never_read_and_export_survives_failed_drain() {
        let t = enabled();
        let token = ready(&t);
        assert_eq!(t.compute(token, 2, 2, 0, 4000, |_, _| (-1, 9)), -1);
        assert!(t.ready.load(Ordering::Acquire));
        struct Failed;
        impl Write for Failed {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> {
                Err(io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        assert!(t.dump(&mut Failed).is_err());
        assert!(t.ready.load(Ordering::Acquire));
        let mut out = Vec::new();
        t.dump(&mut out).unwrap();
        assert!(String::from_utf8(out).unwrap().contains("status=-1"));
        assert!(!t.ready.load(Ordering::Acquire));
    }
    #[test]
    fn actual_compensation_rejects_failed_nonfinite_or_unobserved_results() {
        for (status, factor) in [(-1, f32::NAN), (0, f32::NAN), (0, 0.0)] {
            let t = enabled();
            let radio = Radio {
                sequence: 1,
                timestamp: 1000,
                agc: 32,
                fft: -7,
                length: 4,
                factor,
                ..Radio::default()
            };
            let s = summarize(&[3, 4, 0, 5]);
            t.rf(1, radio);
            t.compensation(1, 32, -7, 1, status, factor, s, s);
            t.row(
                1,
                0,
                1,
                radio,
                &[3, 4, 0, 5],
                &[5.0, 5.0],
                false,
                1,
                s,
                2100,
            );
            assert_eq!(t.enqueue(1, 0, 1, 2, 2, &[1, 0], &[1, 0], 3000), 0);
        }
    }
    #[test]
    fn iq_sampling_matches_signed_bytes_and_packed_12bit_and_bounds() {
        assert_eq!(
            &selected_iq(&[3, 4, 250, 251], 2, 1, false).unwrap()[..4],
            &[3, 4, -6, -5]
        );
        assert_eq!(
            &selected_iq(&[0x30, 0, 0x40, 0], 1, 1, true).unwrap()[..2],
            &[3, 4]
        );
        assert_eq!(
            &selected_iq(&[0xf0, 0xff, 0xe0, 0xff], 1, 1, true).unwrap()[..2],
            &[-1, -2]
        );
        assert!(selected_iq(&[0; 4], 2, 2, false).is_none());
        assert!(selected_iq(&[0; 4], 1, 0, false).is_none());
        let s = summarize(&[0, 1, 0, 2]);
        assert_eq!(s.bytes, 4);
        assert_eq!(s.zeros, 2);
    }
    #[test]
    fn baseline_warmup_is_unknown_not_a_qualified_sample_and_later_loss_fails() {
        let not_collecting = enabled();
        let radio = Radio {
            sequence: 1,
            timestamp: 1000,
            agc: 32,
            fft: -7,
            length: 4,
            factor: 0.0,
            ..Radio::default()
        };
        let bytes = summarize(&[3, 4, 0, 5]);
        not_collecting.rf(1, radio);
        not_collecting.compensation(1, 32, -7, 1, 0x103, f32::NAN, bytes, bytes);
        not_collecting.row(
            1,
            0,
            1,
            radio,
            &[3, 4, 0, 5],
            &[5.0, 5.0],
            false,
            1,
            bytes,
            1100,
        );
        assert_ne!(not_collecting.faults.load(Ordering::Relaxed), 0);
        let t = enabled();
        assert!(t.arm(1));
        let radio = Radio {
            sequence: 1,
            timestamp: 1000,
            agc: 32,
            fft: -7,
            length: 4,
            factor: 0.0,
            observed_lo: 1000,
            ..Radio::default()
        };
        let bytes = summarize(&[3, 4, 0, 5]);
        t.rf(1, radio);
        t.compensation(1, 32, -7, 0, 0x103, f32::NAN, bytes, bytes);
        t.row(
            1,
            0,
            1,
            radio,
            &[3, 4, 0, 5],
            &[5.0, 5.0],
            false,
            1,
            bytes,
            1100,
        );
        let token = t.enqueue(1, 0, 1, 2, 2, &[1, 0], &[1, 0], 2000);
        assert_ne!(token, 0);
        assert_eq!(t.faults.load(Ordering::Relaxed), 0);
        assert_eq!(t.gain_unavailable_rows.load(Ordering::Relaxed), 1);
        assert!(!t.gain_ready.load(Ordering::Acquire));
        t.compute(token, 2, 1, 0, 3000, |a, _| {
            assert_eq!(t.current_tag(), 0);
            assert_eq!(unsafe { std::slice::from_raw_parts(a, 2) }, &[5.0, 5.0]);
            (0, 1)
        });
        decision(&t, token);
        assert!(!t.ready.load(Ordering::Acquire));
        let token = ready(&t);
        assert!(t.gain_ready.load(Ordering::Acquire));
        t.compute(token, 2, 2, 0, 4000, |_, _| {
            assert_eq!(t.current_tag(), 1);
            (0, 2)
        });
        decision(&t, token);
        assert!(t.ready.load(Ordering::Acquire));
        let radio = Radio {
            sequence: 3,
            timestamp: 3000,
            ..radio
        };
        t.rf(3, radio);
        t.compensation(3, 32, -7, 0, 0x103, f32::NAN, bytes, bytes);
        t.row(
            1,
            0,
            3,
            radio,
            &[3, 4, 0, 5],
            &[5.0, 5.0],
            false,
            1,
            bytes,
            3100,
        );
        assert_ne!(t.faults.load(Ordering::Relaxed), 0);
    }
    #[test]
    fn associated_source_is_explicit_and_other_sdk_peers_are_not_captured() {
        let t = Trace::new();
        assert!(!t.prepare());
        assert!(!t.select_source([0; 6]));
        assert!(!t.select_source([255; 6]));
        assert!(t.select_source([1, 2, 3, 4, 5, 6]));
        assert!(t.prepare());
        t.enabled.store(true, Ordering::Release);
        assert!(t.matches(&[1, 2, 3, 4, 5, 6]));
        assert!(!t.matches(&[255; 6]));
        assert!(!t.matches(&[6, 5, 4, 3, 2, 1]));
        assert!(!t.capture_admitted());
        frame(&t, 1, 0, &[5.0, 5.0]);
        assert!(t.gain_admitted(101));
        t.other_frame();
        assert!(!t.gain_admitted(101));
        assert_eq!(t.other_peer_frames.load(Ordering::Relaxed), 1);
        assert!(!t.select_source([2; 6]));
        frame(&t, 2, 1, &[5.0, 5.0]);
        assert!(t.gain_admitted(102));
        assert_eq!(t.faults.load(Ordering::Relaxed), 0);
    }
    #[test]
    fn numerical_task_identity_is_single_and_arm_cannot_retag_an_inflight_window() {
        let t = enabled();
        assert!(t.claim(9));
        assert!(t.claim(9));
        assert!(!t.claim(10));
        assert!(!t.claim(0));
        let token = ready(&t);
        t.compute(token, 2, 2, 0, 4000, |_, _| {
            assert_eq!(t.current_tag(), 0);
            assert!(t.arm(1));
            assert_eq!(t.current_tag(), 0);
            (0, 1)
        });
        decision(&t, token);
        assert!(!t.ready.load(Ordering::Acquire));
        let token = enqueue(&t);
        t.compute(token, 2, 2, 0, 4000, |_, _| {
            assert_eq!(t.current_tag(), 1);
            (0, 2)
        });
        decision(&t, token);
        assert!(t.ready.load(Ordering::Acquire));
    }
    #[test]
    fn trace_arm_export_contention_and_sequence_exhaustion_are_explicit() {
        let t = enabled();
        assert!(!t.arm(0));
        assert!(t.arm(1));
        assert!(!t.arm(2));
        let token = ready(&t);
        t.compute(token, 2, 2, 0, 4000, |_, _| (0, 1));
        let paused = t.exported.lock().unwrap();
        decision(&t, token);
        drop(paused);
        assert_ne!(t.faults.load(Ordering::Relaxed), 0);
        t.next.store(u32::MAX, Ordering::Release);
        assert_eq!(enqueue(&t), 0);
        assert!(!t.prepare());
    }
}
