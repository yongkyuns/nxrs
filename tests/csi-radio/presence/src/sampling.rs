//! Bounded laboratory copies at real SDK callback sites. No numerical work,
//! new workers or policy mutation. Recorder leases join before its footer.
use crate::lab::LAB;
use csi_lab_core::journal;
fn stack_free_bytes() -> u32 {
    extern "C" {
        fn presence_task_stack_free_bytes() -> u32;
    }
    unsafe { presence_task_stack_free_bytes() }
}

#[no_mangle]
pub extern "C" fn presence_sampling_active() -> bool {
    LAB.enabled() && crate::lab::motion_enabled()
}
#[no_mangle]
#[inline(never)]
pub extern "C" fn presence_sampling_ping(
    outcome: u32,
    sequence: u32,
    query_status: i32,
    elapsed_ms: u32,
    sent: u32,
    received: u32,
) {
    if !presence_sampling_active() {
        return;
    }
    let free = stack_free_bytes();
    LAB.push_words(journal::SAMPLING, crate::calibration::now_us(), 12, |w| {
        w.copy_from_slice(&[
            1,
            1,
            outcome,
            sequence,
            query_status as u32,
            elapsed_ms,
            sent,
            received,
            free,
            0,
            0,
            0,
        ]);
    });
    if free < 512 {
        LAB.invalidate();
    }
}
#[no_mangle]
#[inline(never)]
pub unsafe extern "C" fn presence_sampling_window(
    peer: *const u8,
    reason: u32,
    sequence: u32,
    rx_time_ms: u32,
    rows: u32,
    span_ms: i32,
    delta_ms: i32,
    limit_ms: u32,
    handle_window: u32,
    handle_time_ms: u32,
) {
    if !presence_sampling_active() {
        return;
    }
    let Some(peer) = peer.cast::<[u8; 6]>().as_ref() else {
        LAB.invalidate();
        return;
    };
    if !crate::trace::TRACE.matches(peer) {
        return;
    }
    let free = stack_free_bytes();
    LAB.push_words(journal::SAMPLING, crate::calibration::now_us(), 12, |w| {
        w.copy_from_slice(&[
            1,
            2,
            reason,
            sequence,
            rx_time_ms,
            rows,
            span_ms as u32,
            delta_ms as u32,
            limit_ms,
            handle_window,
            handle_time_ms,
            free,
        ]);
    });
    if free < 512 {
        LAB.invalidate();
    }
}
