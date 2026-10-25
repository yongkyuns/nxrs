//! Continuous, opt-in experiment recording. Producers copy owned values only;
//! serialization and USB writes belong to the control task. No GPL sources.
#![cfg_attr(not(target_os = "espidf"), allow(dead_code))]
use csi_lab_core::journal::{self, Operation, Record};
use std::sync::atomic::{AtomicU32, Ordering};
pub static LAB: csi_lab_core::recorder::Recorder = csi_lab_core::recorder::Recorder::new();
static GAINS: AtomicU32 = AtomicU32::new(0);
static WANDERS: AtomicU32 = AtomicU32::new(0);
pub fn record(r: Record) {
    LAB.push(r);
}
pub fn control(op: Operation) -> Option<bool> {
    if !LAB.enabled() {
        return crate::calibration::RUNTIME
            .update(|value| op.apply(value, crate::calibration::now_us()));
    }
    crate::calibration::RUNTIME
        .execute(op, crate::calibration::now_us, record)
        .map(|v| v.0)
}
pub fn poll() -> Result<crate::calibration::Snapshot, crate::calibration::ReadUnavailable> {
    if !LAB.enabled() {
        return crate::calibration::RUNTIME.snapshot(crate::calibration::now_us);
    }
    crate::calibration::RUNTIME
        .execute(Operation::Poll, crate::calibration::now_us, record)
        .map(|v| v.1)
        .ok_or(crate::calibration::ReadUnavailable::Busy)
}
pub fn label(label: u32) -> bool {
    crate::calibration::RUNTIME
        .update(|_| {
            record(Record::new(journal::LABEL, crate::calibration::now_us(), &[label]).unwrap());
        })
        .is_some()
}
fn bytes(
    lease: &csi_lab_core::recorder::Lease<'_>,
    kind: u32,
    now: u64,
    header: &[u32],
    data: &[u8],
) {
    let mut w = [0u32; journal::WORDS];
    if header.len() + data.len().div_ceil(4) > w.len() {
        LAB.invalidate();
        return;
    }
    w[..header.len()].copy_from_slice(header);
    for (i, chunk) in data.chunks(4).enumerate() {
        let mut b = [0u8; 4];
        b[..chunk.len()].copy_from_slice(chunk);
        w[header.len() + i] = u32::from_le_bytes(b);
    }
    lease.push(Record::new(kind, now, &w[..header.len() + data.len().div_ceil(4)]).unwrap());
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
}
pub fn gain_id() -> u32 {
    GAINS.fetch_add(1, Ordering::Relaxed) + 1
}
/// Chunks retain exact f32 bits and row split; matrices stay owned by the
/// original frozen-input caller throughout this synchronous copy.
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
        let mut w = [0u32; journal::WORDS];
        w[..8].copy_from_slice(&[
            id,
            part,
            cols,
            rows0,
            rows1,
            total as u32,
            offset as u32,
            status as u32,
        ]);
        for (to, from) in w[8..8 + n].iter_mut().zip(&values[offset..offset + n]) {
            *to = from.to_bits();
        }
        lease.push(Record::new(kind, now, &w[..8 + n]).unwrap());
    }
}
pub fn wander_id() -> u32 {
    WANDERS.fetch_add(1, Ordering::Relaxed) + 1
}

#[cfg(target_os = "espidf")]
#[no_mangle]
pub static presence_lab_queue_bytes: u32 = (128 * std::mem::size_of::<Record>()) as u32;

pub fn training(command: u32, status: i32, wander: f32, jitter: f32) -> bool {
    if !LAB.enabled() {
        return true;
    }
    crate::calibration::RUNTIME
        .update(|_| {
            record(
                Record::new(
                    journal::CONTROL,
                    crate::calibration::now_us(),
                    &[command, status as u32, wander.to_bits(), jitter.to_bits()],
                )
                .unwrap(),
            )
        })
        .is_some()
}

pub fn source(peer: [u8; 6], channel: u8) {
    let mut bytes = [0u8; 8];
    bytes[..6].copy_from_slice(&peer);
    record(
        Record::new(
            journal::CONTROL,
            crate::calibration::now_us(),
            &[
                3,
                u32::from(channel),
                u32::from_le_bytes(bytes[..4].try_into().unwrap()),
                u32::from_le_bytes(bytes[4..].try_into().unwrap()),
            ],
        )
        .unwrap(),
    );
}
