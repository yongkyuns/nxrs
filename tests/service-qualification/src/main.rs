//! Local qualification, not a replacement for the production service API.
use std::ffi::{c_char, c_int, c_void};

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct Event {
    origin_cycles: u32,
    sequence: u32,
    value: u32,
    source: u16,
    kind: u8,
    reserved: u8,
}
const _: () = assert!(std::mem::size_of::<Event>() == 16);

unsafe extern "C" {
    fn nxrs_sq_run(argc: c_int, argv: *mut *mut c_char) -> c_int;
    fn nxrs_sq_id(service: *mut c_void) -> u32;
    fn nxrs_sq_count(service: *mut c_void) -> u32;
    fn nxrs_sq_ready(service: *mut c_void);
    fn nxrs_sq_wait(service: *mut c_void, pending: *const Event, event: *mut Event) -> c_int;
    fn nxrs_sq_led_open(service: *mut c_void) -> c_int;
    fn nxrs_sq_led_apply(service: *mut c_void, level: u32) -> c_int;
    fn nxrs_sq_led_close(service: *mut c_void) -> c_int;
    fn nxrs_sq_record(service: *mut c_void, event: *const Event);
    fn nxrs_sq_failed(service: *mut c_void);
}

impl Event {
    fn valid(&self) -> bool {
        self.source == 0 && self.reserved == 0 && matches!(self.kind, 1 | 2)
            && self.value == (self.sequence & 1)
    }
}

// SAFETY: the native coordinator exclusively loans one live service handle
// to this callback until it returns. All calls are synchronous and copy events.
#[no_mangle]
unsafe extern "C" fn nxrs_sq_worker(service: *mut c_void) {
    let id = unsafe { nxrs_sq_id(service) };
    let count = unsafe { nxrs_sq_count(service) };
    let led = id == count - 2;
    if led && unsafe { nxrs_sq_led_open(service) } != 0 {
        unsafe { nxrs_sq_failed(service) };
        return;
    }
    unsafe { nxrs_sq_ready(service) };
    let mut pending: Option<Event> = None;
    let mut next_control = 0;
    let mut next_data = 1;
    loop {
        let mut event = Event::default();
        let result = unsafe { nxrs_sq_wait(service,
            pending.as_ref().map_or(std::ptr::null(), |event| event), &mut event) };
        pending = None;
        if result < 0 { unsafe { nxrs_sq_failed(service) }; break; }
        if result == 0 { break; }
        let next = if event.kind == 1 { &mut next_control } else { &mut next_data };
        if !event.valid() || event.sequence != *next
            || (led && unsafe { nxrs_sq_led_apply(service, event.value) } != 0) {
            unsafe { nxrs_sq_failed(service) };
            break;
        }
        *next += if event.kind == 1 { 3 } else if *next % 3 == 1 { 1 } else { 2 };
        unsafe { nxrs_sq_record(service, &event) };
        if id + 1 < count { pending = Some(event); }
    }
    if led && unsafe { nxrs_sq_led_close(service) } != 0 { unsafe { nxrs_sq_failed(service) }; }
}

fn main() -> std::process::ExitCode {
    // Keep argument parsing and diagnostics identical. Rust owns these strings
    // through the synchronous run; neither the coordinator nor workers retain them.
    let strings: Vec<_> = std::env::args().map(|s| std::ffi::CString::new(s).unwrap()).collect();
    let mut arguments: Vec<_> = strings.iter().map(|s| s.as_ptr().cast_mut()).collect();
    arguments.push(std::ptr::null_mut());
    let result = unsafe { nxrs_sq_run(strings.len() as c_int, arguments.as_mut_ptr()) };
    // Returning runs the argument owners' destructors on failure too.
    std::process::ExitCode::from(result as u8)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_malformed_domain_events() {
        for sequence in 0..1024 {
            let event = Event { sequence, value: sequence & 1, kind: 2, ..Event::default() };
            assert!(event.valid());
            assert!(!Event { value: event.value ^ 1, ..event }.valid());
            assert!(!Event { kind: 0, ..event }.valid());
            assert!(!Event { source: 1, ..event }.valid());
            assert!(!Event { reserved: 1, ..event }.valid());
        }
    }
}
