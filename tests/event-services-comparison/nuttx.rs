//! Rust service loops, using the same NuttX queue/thread adapter as C.
#![cfg_attr(target_os = "nuttx", no_main)]
mod controls;
#[path = "core.rs"]
mod protocol;
#[cfg(not(target_os = "nuttx"))]
fn main() {}
#[cfg(target_os = "nuttx")]
mod board {
    use super::protocol::*;
    use std::ffi::{c_char, c_int};
    unsafe extern "C" {
        fn es_now() -> u32;
        fn es_profile() -> u32;
        fn es_runtime_state(id: u32) -> *mut State;
        fn es_platform_send(destination: u32, kind: u32, event: *const Event) -> c_int;
        fn es_platform_wait(id: u32, timeout: u32, event: *mut Event) -> c_int;
        fn es_record_send(id: u32, event: *const Event, result: c_int, posted: u32);
        fn es_record_receive(id: u32, event: *const Event, result: c_int, start: u32, end: u32);
        fn es_record_pending(id: u32, next: u32);
        fn es_run_main(argc: c_int, argv: *const *const c_char) -> c_int;
        fn es_register_rust_loop(callback: unsafe extern "C" fn(u32));
        fn es_control_apply(id: u32, event: *const Event) -> c_int;
    }
    #[unsafe(no_mangle)]
    pub unsafe extern "C" fn es_rust_loop(id: u32) {
        // SAFETY: the coordinator gives each joined pthread a distinct id and
        // state slot, initialized before the start gate; no peer reads it live.
        let state = unsafe { &mut *es_runtime_state(id) };
        let profile = unsafe { es_profile() };
        let mut schedule = Schedule::new(id, profile);
        let stop = (ES_DURATION_US + ES_DRAIN_US) * ES_HZ;
        while unsafe { es_now() } < stop {
            if let Some(release) = schedule.due(unsafe { es_now() }) {
                for n in 0..release.count {
                    let current = Release {
                        sequence: release.sequence + n,
                        ..release
                    };
                    for peer in 0..3 {
                        let mut event = Event::new(id, peer, &current, 0);
                        let posted = unsafe { es_now() };
                        event.posted_cycles = posted;
                        let result = unsafe {
                            es_platform_send(event.destination.into(), event.kind.into(), &event)
                        };
                        unsafe {
                            es_record_send(id, &event, result, posted);
                        }
                    }
                }
            }
            let now = unsafe { es_now() };
            let deadline = match schedule.next_deadline() {
                n if n >= ES_DURATION_US * ES_HZ => stop,
                n => n,
            };
            let mut event = Event::default();
            let available =
                unsafe { es_platform_wait(id, deadline.saturating_sub(now), &mut event) };
            if available < 0 {
                unsafe {
                    es_record_receive(id, &event, -1, 0, 0);
                }
                break;
            }
            if available > 0 {
                let started = unsafe { es_now() };
                let result = if state.handle(&event, id).is_ok() {
                    if id == 0 && event.kind == 1 {
                        state.value = work_value(
                            state.value,
                            event.token,
                            super::controls::work_iterations(profile),
                        );
                    }
                    unsafe { es_control_apply(id, &event) }
                } else {
                    -1
                };
                let finished = unsafe { es_now() };
                unsafe {
                    es_record_receive(id, &event, result, started, finished);
                }
            }
        }
        unsafe {
            es_record_pending(id, schedule.next_deadline());
        }
    }
    #[unsafe(no_mangle)]
    pub unsafe extern "C" fn main(argc: c_int, argv: *const *const c_char) -> c_int {
        // SAFETY: NSH owns argc/argv; the synchronous C coordinator validates
        // them and joins every service before returning.
        // Passing the callback makes its reachability explicit across Cargo's
        // relocatable GC link and the later NuttX final link.
        unsafe {
            es_register_rust_loop(es_rust_loop);
            es_run_main(argc, argv)
        }
    }
}
