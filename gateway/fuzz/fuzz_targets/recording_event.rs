#![no_main]
use libfuzzer_sys::fuzz_target;
fuzz_target!(|data: &[u8]| {
    nxrs_camera_activity::probe::recording_event(data);
});
