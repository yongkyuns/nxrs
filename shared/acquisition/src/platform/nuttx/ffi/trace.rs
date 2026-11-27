//! Borrowed bounded labels copied synchronously by NuttX's native note driver.
unsafe extern "C" {
    fn acquisition_trace_event(edge: u8, label: *const u8, length: usize);
}

pub fn record(edge: u8, label: &[u8]) {
    // SAFETY: the native call copies at most these 96 initialized bytes before
    // returning. The recorder is used by task-context acquisition only.
    unsafe { acquisition_trace_event(edge, label.as_ptr(), label.len().min(96)) };
}
