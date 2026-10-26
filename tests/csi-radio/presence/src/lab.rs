//! Continuous, opt-in experiment recording. Producers copy owned values only;
//! serialization and USB writes belong to the control task. No GPL sources.
#![cfg_attr(not(target_os = "espidf"), allow(dead_code))]
use csi_lab_core::journal::{self, ModelEvent, Operation};
use std::sync::atomic::{AtomicU32, Ordering};
pub static LAB: csi_lab_core::recorder::Recorder = csi_lab_core::recorder::Recorder::new();
static GAINS: AtomicU32 = AtomicU32::new(0);
static WANDERS: AtomicU32 = AtomicU32::new(0);
pub fn model(event: ModelEvent) {
    LAB.push_model(event);
}
pub fn control(op: Operation) -> Option<bool> {
    if !LAB.enabled() {
        return crate::calibration::RUNTIME
            .update(|value| op.apply(value, crate::calibration::now_us()));
    }
    crate::calibration::RUNTIME
        .execute(op, crate::calibration::now_us, model)
        .map(|v| v.0)
}
pub fn poll() -> Result<crate::calibration::Snapshot, crate::calibration::ReadUnavailable> {
    if !LAB.enabled() {
        return crate::calibration::RUNTIME.snapshot(crate::calibration::now_us);
    }
    crate::calibration::RUNTIME
        .execute(Operation::Poll, crate::calibration::now_us, model)
        .map(|v| v.1)
        .ok_or(crate::calibration::ReadUnavailable::Busy)
}
pub fn label(label: u32) -> bool {
    crate::calibration::RUNTIME
        .update(|_| {
            LAB.push_words(journal::LABEL, crate::calibration::now_us(), 1, |words| {
                words[0] = label
            });
        })
        .is_some()
}
#[inline(never)]
fn bytes(
    lease: &csi_lab_core::recorder::Lease<'_>,
    kind: u32,
    now: u64,
    header: &[u32],
    data: &[u8],
) {
    let len = header.len() + data.len().div_ceil(4);
    lease.push_words(kind, now, len, |words| {
        words[..header.len()].copy_from_slice(header);
        for (i, chunk) in data.chunks(4).enumerate() {
            let mut bytes = [0u8; 4];
            bytes[..chunk.len()].copy_from_slice(chunk);
            words[header.len() + i] = u32::from_le_bytes(bytes);
        }
    });
}
#[cfg(target_os = "espidf")]
#[no_mangle]
pub unsafe extern "C" fn presence_lab_rf(
    data: *const u8,
    radio: *const crate::trace::Radio,
    dispatch: *const u8,
    source: *const u8,
) {
    let Some(lease) = LAB.lease() else {
        return;
    };
    let Some(r) = radio.as_ref() else {
        LAB.invalidate();
        return;
    };
    if data.is_null() || dispatch.is_null() || source.is_null() || r.length == 0 || r.length > 612 {
        LAB.invalidate();
        return;
    }
    let mut macs = [0u8; 12];
    macs[..6].copy_from_slice(std::slice::from_raw_parts(dispatch, 6));
    macs[6..].copy_from_slice(std::slice::from_raw_parts(source, 6));
    let h = [
        data as u32,
        r.sequence,
        r.timestamp,
        r.rssi as u32,
        r.agc,
        r.fft as u32,
        r.data_type,
        r.length,
        u32::from_le_bytes(macs[..4].try_into().unwrap()),
        u32::from_le_bytes(macs[4..8].try_into().unwrap()),
        u32::from_le_bytes(macs[8..].try_into().unwrap()),
    ];
    bytes(
        &lease,
        journal::RAW,
        crate::calibration::now_us(),
        &h,
        std::slice::from_raw_parts(data, r.length as usize),
    );
    stack_probe(&RF_CALLS, &RF_STACK);
}
/// Bytes at the actual native gain call boundary, including nonselected peers.
#[cfg(target_os = "espidf")]
pub unsafe fn gain(
    lease: Option<&csi_lab_core::recorder::Lease<'_>>,
    data: *const u8,
    size: usize,
    samples16: bool,
    agc: u8,
    fft: i8,
    state: i32,
    phase: u32,
    id: u32,
    status: i32,
    factor: f32,
) {
    let Some(lease) = lease else {
        return;
    };
    if data.is_null() || size == 0 || size > 612 {
        LAB.invalidate();
        return;
    }
    bytes(
        lease,
        journal::GAIN,
        crate::calibration::now_us(),
        &[
            id,
            data as u32,
            phase,
            status as u32,
            factor.to_bits(),
            state as u32,
            u32::from(agc),
            fft as i32 as u32,
            u32::from(samples16),
            size as u32,
        ],
        std::slice::from_raw_parts(data, size),
    );
    stack_probe(&GAIN_CALLS, &GAIN_STACK);
}
pub fn gain_id() -> u32 {
    GAINS.fetch_add(1, Ordering::Relaxed) + 1
}
/// Chunks retain exact f32 bits and row split; matrices stay owned by the
/// original frozen-input caller throughout this synchronous copy.
#[inline(never)]
pub fn floats(
    lease: Option<&csi_lab_core::recorder::Lease<'_>>,
    kind: u32,
    id: u32,
    part: u32,
    cols: u32,
    rows0: u32,
    rows1: u32,
    status: i32,
    values: &[f32],
) {
    let Some(lease) = lease else {
        return;
    };
    let total = values.len();
    let now = crate::calibration::now_us();
    if total > 4096 {
        LAB.invalidate();
        return;
    }
    for offset in (0..total.max(1)).step_by(240) {
        let n = (total - offset).min(240);
        let header = [
            id,
            part,
            cols,
            rows0,
            rows1,
            total as u32,
            offset as u32,
            status as u32,
        ];
        lease.push_words(kind, now, 8 + n, |words| {
            words[..8].copy_from_slice(&header);
            for (to, from) in words[8..].iter_mut().zip(&values[offset..offset + n]) {
                *to = from.to_bits();
            }
        });
    }
    #[cfg(target_os = "espidf")]
    compute_stack_probe();
}
pub fn wander_id() -> u32 {
    WANDERS.fetch_add(1, Ordering::Relaxed) + 1
}

#[cfg(target_os = "espidf")]
#[no_mangle]
pub static presence_lab_queue_bytes: u32 =
    csi_lab_core::recorder::Recorder::storage_bytes(128) as u32;

pub fn training(command: u32, status: i32, wander: f32, jitter: f32) -> bool {
    if !LAB.enabled() {
        return true;
    }
    crate::calibration::RUNTIME
        .update(|_| {
            LAB.push_words(journal::CONTROL, crate::calibration::now_us(), 4, |words| {
                words.copy_from_slice(&[command, status as u32, wander.to_bits(), jitter.to_bits()])
            })
        })
        .is_some()
}
pub fn source(peer: [u8; 6], channel: u8) {
    let mut bytes = [0u8; 8];
    bytes[..6].copy_from_slice(&peer);
    LAB.push_words(journal::CONTROL, crate::calibration::now_us(), 4, |words| {
        words.copy_from_slice(&[
            3,
            u32::from(channel),
            u32::from_le_bytes(bytes[..4].try_into().unwrap()),
            u32::from_le_bytes(bytes[4..].try_into().unwrap()),
        ])
    });
}

static RF_CALLS: AtomicU32 = AtomicU32::new(0);
static GAIN_CALLS: AtomicU32 = AtomicU32::new(0);
static COMPUTE_CALLS: AtomicU32 = AtomicU32::new(0);
static RF_STACK: AtomicU32 = AtomicU32::new(u32::MAX);
static GAIN_STACK: AtomicU32 = AtomicU32::new(u32::MAX);
static COMPUTE_STACK: AtomicU32 = AtomicU32::new(u32::MAX);
#[cfg(target_os = "espidf")]
fn stack_probe(calls: &AtomicU32, minimum: &AtomicU32) {
    extern "C" {
        fn presence_task_stack_free_bytes() -> u32;
    }
    if calls.fetch_add(1, Ordering::Relaxed) & 63 == 0 {
        let observed = unsafe { presence_task_stack_free_bytes() };
        let old = minimum.load(Ordering::Relaxed);
        if observed < old
            && minimum
                .compare_exchange(old, observed, Ordering::Relaxed, Ordering::Relaxed)
                .is_err()
        {
            LAB.invalidate();
        }
    }
}
pub fn report() {
    let read = |v: &AtomicU32| v.load(Ordering::Relaxed);
    println!(
        "PRESENCE_LAB_STATUS v=1 losses={} slots=128 rf_stack={} gain_stack={} compute_stack={}",
        LAB.loss(),
        read(&RF_STACK),
        read(&GAIN_STACK),
        read(&COMPUTE_STACK)
    );
}

/// Control-task transport. The IDF stdio adapter drops timed-out characters
/// while returning the requested length; direct bulk writes retain counts.
pub struct Output;
impl std::io::Write for Output {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        extern "C" {
            fn reference_usb_write(data: *const u8, length: usize) -> i32;
        }
        let started = std::time::Instant::now();
        csi_lab_core::progress::write_bytes(
            bytes,
            |remaining| {
                let n = unsafe { reference_usb_write(remaining.as_ptr(), remaining.len()) };
                if n < 0 {
                    Err(std::io::Error::other("record USB write failed"))
                } else {
                    Ok(n as usize)
                }
            },
            || started.elapsed() >= std::time::Duration::from_secs(3),
            || std::thread::sleep(std::time::Duration::from_millis(1)),
        )?;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

#[cfg(target_os = "espidf")]
pub fn compute_stack_probe() {
    stack_probe(&COMPUTE_CALLS, &COMPUTE_STACK);
}
