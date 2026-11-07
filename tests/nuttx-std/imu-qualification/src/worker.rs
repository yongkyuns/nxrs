//! Fixture-only NuttX worker identity binding.
#![allow(unsafe_code)]

unsafe extern "C" {
    fn imu_qualification_worker_tid() -> i32;
}

pub(crate) fn current_tid() -> Option<i32> {
    // SAFETY: this test-only C helper returns the calling pthread's NuttX TID.
    let tid = unsafe { imu_qualification_worker_tid() };
    (tid > 0).then_some(tid)
}
