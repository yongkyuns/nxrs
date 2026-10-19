//! Synthetic driver-boundary tests, not physical-camera qualification.
use super::*;
use std::os::fd::FromRawFd;
use std::sync::{Mutex, OnceLock};

#[derive(Default)]
struct Driver {
    address: usize,
    capacity: u32,
    bytes: u32,
    flags: u32,
    pending: bool,
    queue_count: u32,
    close_count: u32,
    format_error: i32,
    on_error: i32,
    off_error: i32,
    close_error: i32,
}

fn driver() -> std::sync::MutexGuard<'static, Driver> {
    static DRIVER: OnceLock<Mutex<Driver>> = OnceLock::new();
    DRIVER
        .get_or_init(|| Mutex::new(Driver::default()))
        .lock()
        .unwrap()
}

#[no_mangle]
extern "C" fn nxrs_camera_open_flags() -> i32 {
    0
}

#[no_mangle]
unsafe extern "C" fn nxrs_camera_format(
    _: i32,
    w: u16,
    h: u16,
    pixels: u8,
    width: *mut u32,
    height: *mut u32,
    actual: *mut u8,
) -> i32 {
    if driver().format_error != 0 {
        return driver().format_error;
    }
    if pixels == 1 {
        return -5;
    } // Model a big-endian RGB565 sensor.
    unsafe {
        *width = u32::from(w);
        *height = u32::from(h);
        *actual = pixels;
    }
    0
}

#[no_mangle]
extern "C" fn nxrs_camera_buffers(_: i32, count: u32) -> i32 {
    assert_eq!(count, 1);
    0
}

#[no_mangle]
unsafe extern "C" fn nxrs_camera_queue(_: i32, data: *mut u8, length: usize) -> i32 {
    assert_eq!(data as usize % 64, 0);
    let mut state = driver();
    state.address = data as usize;
    state.capacity = length as u32;
    state.bytes = length as u32;
    state.queue_count += 1;
    for index in 0..length {
        unsafe {
            *data.add(index) = index as u8;
        }
    }
    0
}

#[no_mangle]
unsafe extern "C" fn nxrs_camera_dequeue(
    _: i32,
    address: *mut usize,
    capacity: *mut u32,
    bytes: *mut u32,
    flags: *mut u32,
) -> i32 {
    let state = driver();
    if state.pending {
        return 1;
    }
    unsafe {
        *address = state.address;
        *capacity = state.capacity;
        *bytes = state.bytes;
        *flags = state.flags;
    }
    0
}

#[no_mangle]
extern "C" fn nxrs_camera_stream(_: i32, on: i32) -> i32 {
    let state = driver();
    if on == 1 {
        state.on_error
    } else {
        state.off_error
    }
}

#[no_mangle]
extern "C" fn rc_nx_close(fd: i32) -> i32 {
    // Consume the real host descriptor even when simulating a driver close error.
    drop(unsafe { File::from_raw_fd(fd) });
    let mut state = driver();
    state.close_count += 1;
    state.close_error
}

const FORMAT: Format = Format {
    width: 2,
    height: 2,
    pixels: PixelFormat::Rgb565,
};

fn camera() -> LiveCamera {
    *driver() = Driver::default();
    LiveCamera::acquire().unwrap()
}

// One test serializes the simulated process-global camera and FFI symbols.
#[test]
fn capture_ownership_validation_and_cleanup() {
    {
        let mut camera = camera();
        assert!(matches!(LiveCamera::acquire(), Err(DeviceError::Busy)));
        assert_eq!(camera.start(FORMAT), Ok(FORMAT));
        assert_eq!(camera.next_poll_at_ms(), Some(0));
        assert_eq!(camera.start(FORMAT), Err(DeviceError::Busy));
        driver().pending = true;
        let mut output = [255; 8];
        assert_eq!(camera.poll_frame(10, &mut output), Ok(None));
        assert_eq!(output, [255; 8]);
        assert_eq!(camera.next_poll_at_ms(), Some(15));
        assert_eq!(
            camera.poll_frame(9, &mut output),
            Err(DeviceError::InvalidData)
        );
        driver().pending = false;
        assert_eq!(
            camera.poll_frame(15, &mut output[..7]),
            Err(DeviceError::Full)
        );
        assert_eq!(driver().queue_count, 1);
        assert_eq!(camera.poll_frame(16, &mut output).unwrap().unwrap().len, 8);
        assert_eq!(output, [1, 0, 3, 2, 5, 4, 7, 6]);
        assert_eq!(driver().queue_count, 1); // No DMA alias while copying.
        assert!(camera.poll_frame(17, &mut output).unwrap().is_some());
        assert_eq!(driver().queue_count, 2);
        camera.stop().unwrap();
        camera.stop().unwrap();
        assert_eq!(driver().close_count, 1);
        assert_eq!(camera.poll_frame(18, &mut output), Err(DeviceError::Io));
        assert_eq!(camera.start(FORMAT), Ok(FORMAT));
        camera.stop().unwrap();
    }
    for fault in 0..4 {
        let mut camera = camera();
        camera.start(FORMAT).unwrap();
        match fault {
            0 => driver().address += 1,
            1 => driver().capacity -= 1,
            2 => driver().bytes -= 1,
            _ => driver().flags = 1,
        }
        assert_eq!(
            camera.poll_frame(0, &mut [0; 8]),
            Err(DeviceError::InvalidData)
        );
        assert_eq!(
            camera.poll_frame(1, &mut [0; 8]),
            Err(DeviceError::InvalidData)
        );
        assert_eq!(driver().queue_count, 1); // A bad dequeue requires explicit stop/restart.
        camera.stop().unwrap();
    }
    {
        let mut camera = camera();
        driver().format_error = -1;
        assert_eq!(camera.start(FORMAT), Err(DeviceError::Unsupported));
        assert_eq!(driver().close_count, 1);
    }
    {
        let mut camera = camera();
        driver().on_error = -6;
        assert_eq!(camera.start(FORMAT), Err(DeviceError::Io));
        assert!(camera.buffer.is_none());
        assert_eq!(driver().close_count, 1);
    }
    {
        let mut camera = camera();
        camera.start(FORMAT).unwrap();
        driver().off_error = -6;
        assert_eq!(camera.stop(), Err(DeviceError::Io));
        assert!(camera.buffer.is_some());
        assert_eq!(driver().close_count, 0);
        driver().off_error = 0;
        camera.stop().unwrap();
        assert!(camera.buffer.is_none());
    }
    {
        let mut camera = camera();
        camera.start(FORMAT).unwrap();
        driver().close_error = -6;
        assert_eq!(camera.stop(), Err(DeviceError::Io));
        assert_eq!(camera.stop(), Err(DeviceError::Io));
        assert_eq!(driver().close_count, 1);
    }
}
