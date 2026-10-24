#![no_main]
use libfuzzer_sys::fuzz_target;
fuzz_target!(|data: &[u8]| {
    nxrs_camera_activity::fuzzing::recording_csv(data);
});
