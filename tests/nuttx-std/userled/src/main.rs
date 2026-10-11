//! Target qualification: real File, native ioctl and upstream USERLED upper half.
//! Only the hardware lower half and the explicit failing node are test fixtures.
use led::{DeviceError, Led};
use std::ffi::c_int;

unsafe extern "C" {
    fn nxrs_userled_fixture_install(phase: c_int) -> c_int;
    fn nxrs_userled_fixture_verify(state: u32, calls: u32) -> c_int;
    fn nxrs_userled_fixture_fds() -> c_int;
}

fn fds() -> c_int {
    // SAFETY: test-only scalar observer of this task group's descriptor table.
    let count = unsafe { nxrs_userled_fixture_fds() };
    assert!(count >= 0, "descriptor observation failed: {count}");
    count
}

fn verify(state: u32, calls: u32) {
    // SAFETY: synchronous read of fixture state, on the sole test owner thread.
    assert_eq!(unsafe { nxrs_userled_fixture_verify(state, calls) }, 0,
               "USERLED lower-half state/call witness disagreed");
}

fn main() {
    println!("NXRS_USERLED_MAIN");
    let mode = std::env::args().nth(1).unwrap_or_default();
    assert!(mode == "pass" || mode == "fail");
    let baseline = fds();
    // No registered test node yet: real std open must fail without leaking.
    assert!(matches!(led::open(), Err(DeviceError::Io)));
    assert_eq!(fds(), baseline);
    // SAFETY: installs only a test-owned, initially absent device path.
    assert_eq!(unsafe { nxrs_userled_fixture_install(0) }, 0);
    // This is an actual NuttX ioctl failure, not a replacement FFI symbol.
    assert!(matches!(led::open(), Err(DeviceError::Io)));
    assert_eq!(fds(), baseline);
    // Verify failing driver's open/ioctl/close, then install upstream USERLED.
    assert_eq!(unsafe { nxrs_userled_fixture_install(1) }, 0);
    let mut calls = 0u32;
    let mut rejects = 0u32;
    const ROUNDS: u32 = 32;
    for _ in 0..ROUNDS {
        let mut leds = led::open().expect("USERLED acquisition");
        assert_eq!(fds(), baseline + 1);
        assert_eq!(leds.supported().bits(), 0x8000_0005);
        leds.set(0, true).unwrap();
        calls += 1;
        verify(1, calls);
        leds.set(31, true).unwrap();
        calls += 1;
        verify(0x8000_0001, calls);
        assert_eq!(leds.set(1, true), Err(DeviceError::Unsupported));
        assert_eq!(leds.set(32, true), Err(DeviceError::InvalidData));
        rejects += 2;
        verify(0x8000_0001, calls);
        leds.set(0, false).unwrap();
        calls += 1;
        verify(0x8000_0000, calls);
        leds.set(31, false).unwrap();
        calls += 1;
        verify(0, calls);
        drop(leds);
        assert_eq!(fds(), baseline);
    }
    if mode == "fail" {
        // The observer must reject an incorrect output state. NSH must also
        // observe failure; a success print followed by a crash is never a pass.
        assert_ne!(unsafe { nxrs_userled_fixture_verify(1, calls) }, 0);
        println!("NXRS_USERLED_INJECTED_FAILURE");
        std::process::exit(7);
    }
    println!("NXRS_USERLED_REPORT rounds={ROUNDS} controls={calls} rejects={rejects} failed_query=1");
}
