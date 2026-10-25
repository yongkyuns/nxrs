//! Test-only numerical witnesses around the unchanged upstream binary calls.
//! The native wrappers do not allocate, print or substitute numerical results.
use std::sync::atomic::{AtomicI32, AtomicU32, Ordering};
static WINDOWS: AtomicU32 = AtomicU32::new(0);
static BAD_INPUT: AtomicU32 = AtomicU32::new(0);
static BAD_OUTPUT: AtomicU32 = AtomicU32::new(0);
static KERNEL_ERRORS: AtomicU32 = AtomicU32::new(0);
static LAST_ERROR: AtomicI32 = AtomicI32::new(0);
static ERROR_COLS: AtomicU32 = AtomicU32::new(0);
static ERROR_ROWS0: AtomicU32 = AtomicU32::new(0);
static ERROR_ROWS1: AtomicU32 = AtomicU32::new(0);
static UNINSPECTED: AtomicU32 = AtomicU32::new(0);
static WANDER_CALLS: AtomicU32 = AtomicU32::new(0);
static BAD_TEMPLATE: AtomicU32 = AtomicU32::new(0);
static BAD_CURRENT: AtomicU32 = AtomicU32::new(0);
static CONSTANT_TEMPLATE: AtomicU32 = AtomicU32::new(0);
static CONSTANT_CURRENT: AtomicU32 = AtomicU32::new(0);

pub fn report() {
    let read = |counter: &AtomicU32| counter.load(Ordering::Relaxed);
    println!("PRESENCE_PROBE windows={} bad_input={} bad_output={} kernel_errors={} uninspected={} wander_calls={} bad_template={} bad_current={} constant_template={} constant_current={} last_error={} error_cols={} error_rows0={} error_rows1={}",
        read(&WINDOWS),read(&BAD_INPUT),read(&BAD_OUTPUT),read(&KERNEL_ERRORS),read(&UNINSPECTED),
        read(&WANDER_CALLS),read(&BAD_TEMPLATE),read(&BAD_CURRENT),read(&CONSTANT_TEMPLATE),read(&CONSTANT_CURRENT),LAST_ERROR.load(Ordering::Relaxed),
        read(&ERROR_COLS),read(&ERROR_ROWS0),read(&ERROR_ROWS1));
}

fn characteristics(values: &[f32]) -> (bool, bool) {
    let finite = values.iter().all(|value| value.is_finite());
    let constant = finite && !values.is_empty() && values.iter().all(|value| *value == values[0]);
    (finite, constant)
}

/// Invoke the unchanged SDK once; failed/null outputs remain unavailable and
/// are never dereferenced. NaN is an explicit unavailable factor on the wire.
unsafe fn compensation(
    output: *mut f32,
    call: impl FnOnce(*mut f32) -> i32,
    observed: impl FnOnce(i32, f32),
) -> i32 {
    let status = call(output);
    let factor = if status == 0 && !output.is_null() {
        *output
    } else {
        f32::NAN
    };
    observed(status, factor);
    status
}
#[cfg(target_os = "espidf")]
pub fn window_count() -> u32 {
    WINDOWS.load(Ordering::Relaxed)
}

#[cfg(target_os = "espidf")]
mod native {
    use super::*;
    extern "C" {
        fn presence_task_identity() -> *mut std::ffi::c_void;
        fn esp_timer_get_time() -> i64;
        fn esp_csi_gain_ctrl_get_gain_status() -> i32;
        fn __real_esp_csi_gain_ctrl_compensate_rx_gain(
            data: *mut std::ffi::c_void,
            size: u16,
            samples_are_16bit: bool,
            output: *mut f32,
            agc: u8,
            fft: i8,
        ) -> i32;
        fn __real_esp_radar_motion_dec_compute(
            cols: u32,
            rows0: u32,
            data0: *const f32,
            rows1: u32,
            data1: *const f32,
            output: *mut f32,
        ) -> i32;
        fn __real_esp_radar_motion_dec_wander_compute(
            a: *const f32,
            b: *const f32,
            len: usize,
        ) -> f32;
    }
    #[no_mangle]
    unsafe extern "C" fn __wrap_esp_radar_motion_dec_compute(
        cols: u32,
        rows0: u32,
        data0: *const f32,
        rows1: u32,
        data1: *const f32,
        output: *mut f32,
    ) -> i32 {
        let window = WINDOWS.fetch_add(1, Ordering::Relaxed) + 1;
        let shape = rows0
            .checked_add(rows1)
            .and_then(|rows| cols.checked_mul(rows));
        let inspect = cols > 0
            && shape.is_some_and(|size| size > 0 && size <= 4096)
            && !output.is_null()
            && (rows0 == 0 || !data0.is_null())
            && (rows1 == 0 || !data1.is_null());
        if inspect {
            let part0 = if rows0 == 0 {
                &[]
            } else {
                std::slice::from_raw_parts(data0, (rows0 * cols) as usize)
            };
            let part1 = if rows1 == 0 {
                &[]
            } else {
                std::slice::from_raw_parts(data1, (rows1 * cols) as usize)
            };
            if !characteristics(part0).0 || !characteristics(part1).0 {
                BAD_INPUT.fetch_add(1, Ordering::Relaxed);
            }
        } else {
            UNINSPECTED.fetch_add(1, Ordering::Relaxed);
            crate::failures::FAILURES.uninspected();
        }
        let task = if crate::capture::RECORDER.enabled.load(Ordering::Acquire)
            || crate::failures::FAILURES.enabled()
        {
            presence_task_identity() as usize
        } else {
            0
        };
        let invoke = |a: *const f32, b: *const f32| {
            if inspect
                && crate::capture::RECORDER.enabled.load(Ordering::Acquire)
                && crate::trace::TRACE.capture_admitted()
            {
                let part0 = if rows0 == 0 {
                    &[]
                } else {
                    std::slice::from_raw_parts(a, (rows0 * cols) as usize)
                };
                let part1 = if rows1 == 0 {
                    &[]
                } else {
                    std::slice::from_raw_parts(b, (rows1 * cols) as usize)
                };
                crate::capture::RECORDER.motion(
                    task,
                    window,
                    esp_timer_get_time() as u64,
                    cols as usize,
                    rows0 as usize,
                    rows1 as usize,
                    part0,
                    part1,
                    output,
                    |a, b| __real_esp_radar_motion_dec_compute(cols, rows0, a, rows1, b, output),
                )
            } else {
                __real_esp_radar_motion_dec_compute(cols, rows0, a, rows1, b, output)
            }
        };
        let status = if inspect && crate::failures::FAILURES.enabled() {
            let part0 = if rows0 == 0 {
                &[]
            } else {
                std::slice::from_raw_parts(data0, (rows0 * cols) as usize)
            };
            let part1 = if rows1 == 0 {
                &[]
            } else {
                std::slice::from_raw_parts(data1, (rows1 * cols) as usize)
            };
            crate::failures::FAILURES.compute(
                crate::trace::TRACE.numerical_source(task),
                task,
                window,
                esp_timer_get_time() as u64,
                cols as usize,
                rows0 as usize,
                rows1 as usize,
                part0,
                part1,
                output,
                invoke,
            )
        } else {
            invoke(data0, data1)
        };
        if status != 0 {
            LAST_ERROR.store(status, Ordering::Relaxed);
            ERROR_COLS.store(cols, Ordering::Relaxed);
            ERROR_ROWS0.store(rows0, Ordering::Relaxed);
            ERROR_ROWS1.store(rows1, Ordering::Relaxed);
            KERNEL_ERRORS.fetch_add(1, Ordering::Relaxed);
        }
        if status == 0
            && inspect
            && !characteristics(std::slice::from_raw_parts(output, cols as usize)).0
        {
            BAD_OUTPUT.fetch_add(1, Ordering::Relaxed);
        }
        status
    }
    #[no_mangle]
    unsafe extern "C" fn __wrap_esp_radar_motion_dec_wander_compute(
        a: *const f32,
        b: *const f32,
        len: usize,
    ) -> f32 {
        WANDER_CALLS.fetch_add(1, Ordering::Relaxed);
        if len > 0 && len <= 256 && !a.is_null() && !b.is_null() {
            let (finite, constant) = characteristics(std::slice::from_raw_parts(a, len));
            if !finite {
                BAD_TEMPLATE.fetch_add(1, Ordering::Relaxed);
            }
            if constant {
                CONSTANT_TEMPLATE.fetch_add(1, Ordering::Relaxed);
            }
            let (finite, constant) = characteristics(std::slice::from_raw_parts(b, len));
            if !finite {
                BAD_CURRENT.fetch_add(1, Ordering::Relaxed);
            }
            if constant {
                CONSTANT_CURRENT.fetch_add(1, Ordering::Relaxed);
            }
        } else {
            UNINSPECTED.fetch_add(1, Ordering::Relaxed);
        }
        if crate::capture::RECORDER.enabled.load(Ordering::Acquire)
            && crate::trace::TRACE.capture_admitted()
            && len > 0
            && len <= 256
            && !a.is_null()
            && !b.is_null()
        {
            crate::capture::RECORDER.wander(
                presence_task_identity() as usize,
                std::slice::from_raw_parts(a, len),
                std::slice::from_raw_parts(b, len),
                |a, b| __real_esp_radar_motion_dec_wander_compute(a, b, len),
            )
        } else {
            __real_esp_radar_motion_dec_wander_compute(a, b, len)
        }
    }
    #[no_mangle]
    unsafe extern "C" fn __wrap_esp_csi_gain_ctrl_compensate_rx_gain(
        data: *mut std::ffi::c_void,
        size: u16,
        samples_are_16bit: bool,
        output: *mut f32,
        agc: u8,
        fft: i8,
    ) -> i32 {
        let admitted = !crate::trace::TRACE.enabled.load(Ordering::Acquire)
            || crate::trace::TRACE.gain_admitted(data as usize);
        let gain_state = if admitted && crate::trace::TRACE.enabled.load(Ordering::Acquire) {
            esp_csi_gain_ctrl_get_gain_status()
        } else {
            -1
        };
        let before = if admitted {
            crate::trace::byte_summary(data.cast(), size as usize)
        } else {
            crate::trace::Summary::default()
        };
        let status = compensation(
            output,
            |output| {
                __real_esp_csi_gain_ctrl_compensate_rx_gain(
                    data,
                    size,
                    samples_are_16bit,
                    output,
                    agc,
                    fft,
                )
            },
            |status, factor| {
                if admitted && crate::capture::RECORDER.enabled.load(Ordering::Acquire) {
                    crate::capture::RECORDER.gain(agc, fft, status, factor);
                }
                if admitted {
                    crate::trace::TRACE.compensation(
                        data as usize,
                        agc,
                        fft,
                        gain_state,
                        status,
                        factor,
                        before,
                        crate::trace::byte_summary(data.cast(), size as usize),
                    );
                }
            },
        );
        status
    }
}

#[cfg(test)]
mod tests {
    use super::{characteristics, compensation};
    #[test]
    fn compensation_preserves_call_status_and_never_reads_failed_output() {
        let mut output = std::mem::MaybeUninit::<f32>::uninit();
        let mut calls = 0;
        unsafe {
            assert_eq!(
                compensation(
                    output.as_mut_ptr(),
                    |_| {
                        calls += 1;
                        -1
                    },
                    |s, f| {
                        assert_eq!(s, -1);
                        assert!(f.is_nan());
                    }
                ),
                -1
            );
            assert_eq!(
                compensation(std::ptr::null_mut(), |_| 0, |_, f| assert!(f.is_nan())),
                0
            );
            assert_eq!(
                compensation(
                    output.as_mut_ptr(),
                    |p| {
                        *p = 0.5;
                        0
                    },
                    |s, f| {
                        assert_eq!(s, 0);
                        assert_eq!(f, 0.5);
                    }
                ),
                0
            );
        }
        assert_eq!(calls, 1);
    }
    #[test]
    fn nonfinite_and_degenerate_vectors_are_distinct() {
        assert_eq!(characteristics(&[1.0, 2.0, 3.0]), (true, false));
        assert_eq!(characteristics(&[0.0; 13]), (true, true));
        assert_eq!(characteristics(&[f32::INFINITY, 1.0]), (false, false));
        assert_eq!(characteristics(&[f32::NAN, 1.0]), (false, false));
        assert_eq!(characteristics(&[]), (true, false));
    }
}
