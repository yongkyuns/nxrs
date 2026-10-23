//! Test-only numerical witnesses around the unchanged upstream binary calls.
//! No allocation, output, threshold changes or substituted numerical results.
use std::sync::atomic::{AtomicU32, Ordering};
static WINDOWS: AtomicU32 = AtomicU32::new(0);
static BAD_INPUT: AtomicU32 = AtomicU32::new(0);
static BAD_OUTPUT: AtomicU32 = AtomicU32::new(0);
static KERNEL_ERRORS: AtomicU32 = AtomicU32::new(0);
static UNINSPECTED: AtomicU32 = AtomicU32::new(0);
static WANDER_CALLS: AtomicU32 = AtomicU32::new(0);
static BAD_TEMPLATE: AtomicU32 = AtomicU32::new(0);
static BAD_CURRENT: AtomicU32 = AtomicU32::new(0);
static CONSTANT_TEMPLATE: AtomicU32 = AtomicU32::new(0);
static CONSTANT_CURRENT: AtomicU32 = AtomicU32::new(0);

pub fn report() {
    let read = |counter: &AtomicU32| counter.load(Ordering::Relaxed);
    println!("PRESENCE_PROBE windows={} bad_input={} bad_output={} kernel_errors={} uninspected={} wander_calls={} bad_template={} bad_current={} constant_template={} constant_current={}",
        read(&WINDOWS),read(&BAD_INPUT),read(&BAD_OUTPUT),read(&KERNEL_ERRORS),read(&UNINSPECTED),
        read(&WANDER_CALLS),read(&BAD_TEMPLATE),read(&BAD_CURRENT),read(&CONSTANT_TEMPLATE),read(&CONSTANT_CURRENT));
}

fn characteristics(values: &[f32]) -> (bool, bool) {
    let finite = values.iter().all(|value| value.is_finite());
    let constant = finite && !values.is_empty() && values.iter().all(|value| *value == values[0]);
    (finite, constant)
}

#[cfg(target_os = "espidf")]
mod native {
    use super::*;
    extern "C" {
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
        WINDOWS.fetch_add(1, Ordering::Relaxed);
        let shape = cols.checked_mul(rows0.saturating_add(rows1));
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
        }
        let status = __real_esp_radar_motion_dec_compute(cols, rows0, data0, rows1, data1, output);
        if status != 0 {
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
        __real_esp_radar_motion_dec_wander_compute(a, b, len)
    }
}

#[cfg(test)]
mod tests {
    use super::characteristics;
    #[test]
    fn nonfinite_and_degenerate_vectors_are_distinct() {
        assert_eq!(characteristics(&[1.0, 2.0, 3.0]), (true, false));
        assert_eq!(characteristics(&[0.0; 13]), (true, true));
        assert_eq!(characteristics(&[f32::INFINITY, 1.0]), (false, false));
        assert_eq!(characteristics(&[f32::NAN, 1.0]), (false, false));
        assert_eq!(characteristics(&[]), (true, false));
    }
}
