//! Target ABI and descriptor-lifecycle regressions, outside portable code.
mod storage;
#[cfg(feature = "std-integration")]
pub(crate) mod std_storage;

use core::slice;
use camera::{Camera, DeviceError, Format, PixelFormat};
use nuttx_support::DeviceCamera;

extern "C" {
    fn rc_target_qualify() -> i32;
    fn rc_test_close_prepare() -> i32;
    fn rc_test_close_arm() -> i32;
    fn rc_test_close_check() -> i32;
    fn rc_test_close_finish() -> i32;
}

pub fn run() {
    // SAFETY: synchronous target fixture, with no retained Rust references.
    assert_eq!(unsafe { rc_target_qualify() }, 0, "target ABI/preemption checks");
    close_failure();
    storage::run();
}

fn close_failure() {
    // SAFETY: these test-only hooks register a target probe and scope fault
    // injection to this thread. They retain no Rust pointers or references.
    assert_eq!(unsafe { rc_test_close_prepare() }, 0);
    let format = Format { width: 2, height: 2, pixels: PixelFormat::Gray8 };
    let mut camera = DeviceCamera::new(c"/dev/nxrs-close-probe");
    assert_eq!(camera.start(format), Ok(format));
    assert_eq!(unsafe { rc_test_close_arm() }, 0);

    // The real target close succeeds, a sentinel reuses that exact descriptor,
    // and the test wrapper returns an injected error. No OS resource leak is
    // required to exercise NuttX's consumed-descriptor/error semantics.
    assert_eq!(camera.stop(), Err(DeviceError::Io));
    assert_eq!(unsafe { rc_test_close_check() }, 0);
    for _ in 0..4 {
        let mut bytes = [0xa5; 4];
        assert_eq!(camera.poll_frame(1, &mut bytes), Err(DeviceError::Io));
        assert_eq!(bytes, [0xa5; 4]);
        assert_eq!(camera.start(format), Err(DeviceError::Busy));
        assert_eq!(camera.stop(), Err(DeviceError::Io));
        assert_eq!(unsafe { rc_test_close_check() }, 0);
    }
    drop(camera);
    // The test verifies survival after Drop too, then deliberately closes the
    // sentinel and proves its negative control detects the closed descriptor.
    assert_eq!(unsafe { rc_test_close_finish() }, 0);
}

/// Exercise C -> Rust -> C -> Rust -> C using pointer, size_t, and u64 values.
///
/// # Safety
/// bytes must address length writable bytes exclusively for this invocation.
/// callback must synchronously obey that same extent and retain no pointers.
#[no_mangle]
pub unsafe extern "C" fn rc_rust_abi_probe(
    bytes: *mut u8,
    length: usize,
    tag: u64,
    callback: unsafe extern "C" fn(*mut u8, usize, u64) -> u64,
) -> u64 {
    if bytes.is_null() || length != 17 || tag != 0x1122_3344_5566_7788 {
        return 0;
    }
    {
        // SAFETY: extent and exclusive access guaranteed by the C fixture.
        let buffer = unsafe { slice::from_raw_parts_mut(bytes, length) };
        for (index, byte) in buffer.iter_mut().enumerate() {
            *byte = index as u8 ^ 0x5a;
        }
    }
    // The temporary Rust borrow above ends BEFORE the C callback mutates it.
    // SAFETY: callback contract is part of this function's caller obligation.
    let result = unsafe { callback(bytes, length, tag) };
    result.rotate_left(7) ^ tag
}
