//! Synthetic driver-boundary tests, not physical-camera qualification.
use super::*;
use std::collections::VecDeque;
use std::os::fd::FromRawFd;
use std::sync::{Mutex, OnceLock};
use std::vec::Vec;

#[derive(Clone, Copy, Default)]
struct BufferState {
    address: usize,
    capacity: u32,
    bytes: u32,
    flags: u32,
}

#[derive(Clone, Copy)]
struct Dequeued {
    index: u32,
    address: usize,
    capacity: u32,
    bytes: u32,
    flags: u32,
}

#[derive(Default)]
struct Driver {
    buffers: [BufferState; BUFFER_COUNT],
    queue_order: VecDeque<u32>,
    dequeued_indices: Vec<u32>,
    dequeue_override: Option<Dequeued>,
    pending: bool,
    queue_calls: u32,
    queue_count: u32,
    queue_error_at_call: Option<u32>,
    dequeue_count: u32,
    close_count: u32,
    stream_on_count: u32,
    stream_off_count: u32,
    setup_calls: Vec<&'static str>,
    quality_requests: Vec<u8>,
    quality_error: i32,
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
    let mut state = driver();
    state.setup_calls.push("format");
    if state.format_error != 0 {
        return state.format_error;
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
extern "C" fn nxrs_camera_quality(_: i32, quality: u8) -> i32 {
    let mut state = driver();
    state.setup_calls.push("quality");
    state.quality_requests.push(quality);
    state.quality_error
}

#[no_mangle]
extern "C" fn nxrs_camera_buffers(_: i32, count: u32) -> i32 {
    assert_eq!(count, BUFFER_COUNT as u32);
    driver().setup_calls.push("buffers");
    0
}

#[no_mangle]
unsafe extern "C" fn nxrs_camera_queue(_: i32, index: u32, data: *mut u8, length: usize) -> i32 {
    assert_eq!(data as usize % 64, 0);
    let mut state = driver();
    state.setup_calls.push("queue");
    state.queue_calls += 1;
    if state.queue_error_at_call == Some(state.queue_calls) {
        return -6;
    }
    let buffer = &mut state.buffers[index as usize];
    buffer.address = data as usize;
    buffer.capacity = length as u32;
    buffer.bytes = length as u32;
    buffer.flags = 0;
    state.queue_count += 1;
    let byte = state.queue_count as u8;
    for offset in 0..length {
        unsafe {
            *data.add(offset) = byte.wrapping_add(offset as u8);
        }
    }
    state.queue_order.push_back(index);
    0
}

#[no_mangle]
unsafe extern "C" fn nxrs_camera_dequeue(
    _: i32,
    index: *mut u32,
    address: *mut usize,
    capacity: *mut u32,
    bytes: *mut u32,
    flags: *mut u32,
) -> i32 {
    let mut state = driver();
    if state.pending {
        return 1;
    }
    let Some(queued_index) = state.queue_order.pop_front() else {
        return 1;
    };
    state.dequeue_count += 1;
    let buffer = state.buffers[queued_index as usize];
    let result = state.dequeue_override.take().unwrap_or(Dequeued {
        index: queued_index,
        address: buffer.address,
        capacity: buffer.capacity,
        bytes: buffer.bytes,
        flags: buffer.flags,
    });
    state.dequeued_indices.push(result.index);
    unsafe {
        *index = result.index;
        *address = result.address;
        *capacity = result.capacity;
        *bytes = result.bytes;
        *flags = result.flags;
    }
    0
}

#[no_mangle]
extern "C" fn nxrs_camera_stream(_: i32, on: i32) -> i32 {
    let mut state = driver();
    if on == 1 {
        state.setup_calls.push("stream_on");
        state.stream_on_count += 1;
        state.on_error
    } else {
        state.stream_off_count += 1;
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

const JPEG_FORMAT: Format = Format {
    pixels: PixelFormat::Jpeg,
    ..FORMAT
};

fn camera() -> LiveCamera {
    *driver() = Driver::default();
    LiveCamera::acquire().unwrap()
}

fn start_jpeg(camera: &mut LiveCamera) {
    assert_eq!(camera.start(JPEG_FORMAT), Ok(JPEG_FORMAT));
    let state = driver();
    assert_eq!(state.queue_count, 2);
    assert_eq!(state.queue_order, VecDeque::from([0, 1]));
    assert_eq!(state.stream_on_count, 1);
    assert!(state.quality_requests.is_empty());
}

// One test serializes the simulated process-global camera and FFI symbols.
#[test]
fn fifo_buffer_ownership_validation_and_cleanup() {
    let quality = JpegQuality::new(81).unwrap();
    let higher = JpegQuality::new(100).unwrap();
    assert_eq!(
        crate::mock::MockCamera::new([]).set_jpeg_quality(quality),
        Err(DeviceError::Unsupported)
    );
    assert_eq!(crate::mock::MockCamera::new([]).discarded_frames(), None);

    // Alternating DQBUF indices and copy-before-requeue: the fake QBUF overwrites
    // the just-consumed allocation, so the caller's copy must already be stable.
    {
        let mut camera = camera();
        assert!(matches!(LiveCamera::acquire(), Err(DeviceError::Busy)));
        start_jpeg(&mut camera);
        assert_eq!(
            driver().setup_calls.as_slice(),
            &["format", "buffers", "queue", "queue", "stream_on"]
        );
        assert_eq!(camera.next_poll_at_ms(), Some(0));
        assert_eq!(camera.start(JPEG_FORMAT), Err(DeviceError::Busy));
        let mut output = [0; 8];
        let capture = camera.poll_frame(10, &mut output).unwrap().unwrap();
        assert_eq!(capture.len, 8);
        assert_eq!(output, [1, 2, 3, 4, 5, 6, 7, 8]);
        {
            let state = driver();
            assert_eq!(state.dequeued_indices.as_slice(), &[0]);
            assert_eq!(state.queue_count, 3);
            assert_eq!(state.queue_order, VecDeque::from([1, 0]));
        }
        camera.poll_frame(11, &mut output).unwrap().unwrap();
        assert_eq!(output, [2, 3, 4, 5, 6, 7, 8, 9]);
        {
            let state = driver();
            assert_eq!(state.dequeued_indices.as_slice(), &[0, 1]);
            assert_eq!(state.queue_count, 4);
            assert_eq!(state.queue_order, VecDeque::from([0, 1]));
        }
        camera.stop().unwrap();
        camera.stop().unwrap();
        assert_eq!(driver().close_count, 1);
        assert_eq!(camera.poll_frame(12, &mut output), Err(DeviceError::Io));
    }

    // An explicit request is applied after format negotiation and before buffers.
    // Busy setters cannot change it, and stop/restart reapplies the cached value.
    {
        let mut camera = camera();
        assert_eq!(camera.set_jpeg_quality(quality), Ok(()));
        assert!(driver().quality_requests.is_empty());
        assert_eq!(camera.start(JPEG_FORMAT), Ok(JPEG_FORMAT));
        assert_eq!(
            driver().setup_calls.as_slice(),
            &[
                "format",
                "quality",
                "buffers",
                "queue",
                "queue",
                "stream_on"
            ]
        );
        assert_eq!(driver().quality_requests.as_slice(), &[81]);
        assert_eq!(camera.set_jpeg_quality(higher), Err(DeviceError::Busy));
        assert_eq!(camera.jpeg_quality, Some(quality));
        driver().off_error = -6;
        assert_eq!(camera.stop(), Err(DeviceError::Io));
        assert_eq!(camera.set_jpeg_quality(higher), Err(DeviceError::Busy));
        driver().off_error = 0;
        camera.stop().unwrap();
        assert_eq!(camera.jpeg_quality, Some(quality));
        assert_eq!(camera.start(JPEG_FORMAT), Ok(JPEG_FORMAT));
        assert_eq!(driver().quality_requests.as_slice(), &[81, 81]);
        camera.stop().unwrap();
        assert_eq!(camera.set_jpeg_quality(higher), Ok(()));
        assert_eq!(camera.jpeg_quality, Some(higher));
    }

    // Control failures clean up before any buffers or DMA can be started, while
    // preserving the request for a later start retry.
    for (code, expected) in [
        (-1, DeviceError::Unsupported),
        (-5, DeviceError::InvalidData),
        (-6, DeviceError::Io),
    ] {
        let mut camera = camera();
        camera.set_jpeg_quality(quality).unwrap();
        driver().quality_error = code;
        assert_eq!(camera.start(JPEG_FORMAT), Err(expected));
        assert!(camera.file.is_none());
        assert!(camera.buffers.iter().all(Option::is_none));
        assert_eq!(camera.jpeg_quality, Some(quality));
        {
            let state = driver();
            assert_eq!(state.setup_calls.as_slice(), &["format", "quality"]);
            assert_eq!(state.queue_count, 0);
            assert_eq!(state.stream_on_count, 0);
            assert_eq!(state.stream_off_count, 0);
            assert_eq!(state.close_count, 1);
        }
        driver().quality_error = 0;
        assert_eq!(camera.start(JPEG_FORMAT), Ok(JPEG_FORMAT));
        assert_eq!(driver().quality_requests.as_slice(), &[81, 81]);
        camera.stop().unwrap();
    }

    // EAGAIN keeps both buffers queued, advertises a retry, and preserves the
    // monotonic observation-time check.
    {
        let mut camera = camera();
        start_jpeg(&mut camera);
        driver().pending = true;
        let mut output = [255; 8];
        assert_eq!(camera.poll_frame(10, &mut output), Ok(None));
        assert_eq!(camera.next_poll_at_ms(), Some(15));
        assert_eq!(camera.queued, [true, true]);
        assert_eq!(driver().queue_count, 2);
        assert_eq!(
            camera.poll_frame(9, &mut output),
            Err(DeviceError::InvalidData)
        );
        driver().pending = false;
        assert!(camera.poll_frame(15, &mut output).unwrap().is_some());
        camera.stop().unwrap();
    }

    // A short destination keeps the dequeued allocation out of DMA until retry.
    // The other queued buffer remains pending, so the backlog is bounded at one.
    {
        let mut camera = camera();
        start_jpeg(&mut camera);
        let mut output = [0; 8];
        assert_eq!(
            camera.poll_frame(0, &mut output[..7]),
            Err(DeviceError::Full)
        );
        assert_eq!(camera.ready.unwrap().index, 0);
        assert_eq!(camera.queued, [false, true]);
        assert_eq!(driver().dequeue_count, 1);
        assert_eq!(driver().queue_count, 2);
        assert_eq!(driver().queue_order, VecDeque::from([1]));
        camera.poll_frame(1, &mut output).unwrap().unwrap();
        assert_eq!(output, [1, 2, 3, 4, 5, 6, 7, 8]);
        assert_eq!(driver().dequeue_count, 1);
        camera.poll_frame(2, &mut output).unwrap().unwrap();
        assert_eq!(driver().dequeued_indices.as_slice(), &[0, 1]);
        camera.stop().unwrap();
    }

    // Even flagged frames must have trustworthy ownership and bounded lengths.
    // Unknown normalized flags still fault closed.
    for invalid in 0..5 {
        let mut camera = camera();
        start_jpeg(&mut camera);
        let buffer = driver().buffers[0];
        driver().dequeue_override = Some(Dequeued {
            index: if invalid == 0 { BUFFER_COUNT as u32 } else { 0 },
            address: if invalid == 1 {
                buffer.address + 1
            } else {
                buffer.address
            },
            capacity: if invalid == 2 {
                buffer.capacity - 1
            } else {
                buffer.capacity
            },
            bytes: if invalid == 3 {
                buffer.capacity + 1
            } else {
                buffer.bytes
            },
            flags: if invalid == 4 { 2 } else { 1 },
        });
        assert_eq!(
            camera.poll_frame(0, &mut [0; 8]),
            Err(DeviceError::InvalidData)
        );
        assert_eq!(
            camera.poll_frame(1, &mut [0; 8]),
            Err(DeviceError::InvalidData)
        );
        assert_eq!(driver().queue_count, 2);
        camera.stop().unwrap();
    }

    // Recoverable errors return ownership without reading even a zero-length
    // payload. One dequeue per poll bounds work; later valid frames still flow.
    {
        let mut camera = camera();
        start_jpeg(&mut camera);
        {
            let mut state = driver();
            for buffer in &mut state.buffers {
                buffer.flags = 1;
                buffer.bytes = 0;
            }
        }
        let mut output = [255; 8];
        for now in [10, 15] {
            assert_eq!(camera.poll_frame(now, &mut output), Ok(None));
            assert_eq!(output, [255; 8]);
            assert_eq!(camera.next_poll_at_ms(), Some(now + 5));
            assert_eq!(camera.queued, [true, true]);
        }
        assert_eq!(camera.discarded_frames(), Some(2));
        assert_eq!(driver().dequeue_count, 2);
        assert!(camera.poll_frame(20, &mut output).unwrap().is_some());
        assert_eq!(output, [3, 4, 5, 6, 7, 8, 9, 10]);
        camera.stop().unwrap();
        assert_eq!(camera.discarded_frames(), Some(2));
        assert_eq!(camera.start(JPEG_FORMAT), Ok(JPEG_FORMAT));
        assert_eq!(camera.discarded_frames(), Some(0));
        camera.stop().unwrap();
    }

    // A short unflagged frame is still malformed, not a recoverable driver report.
    {
        let mut camera = camera();
        start_jpeg(&mut camera);
        driver().buffers[0].bytes = 0;
        assert_eq!(
            camera.poll_frame(0, &mut [0; 8]),
            Err(DeviceError::InvalidData)
        );
        assert_eq!(camera.discarded_frames(), Some(0));
        assert_eq!(driver().queue_count, 2);
        camera.stop().unwrap();
    }

    // Recoverable data status must not hide a failure to return DMA ownership.
    {
        let mut camera = camera();
        start_jpeg(&mut camera);
        driver().buffers[0].flags = 1;
        driver().queue_error_at_call = Some(3);
        let mut output = [255; 8];
        assert_eq!(camera.poll_frame(0, &mut output), Err(DeviceError::Io));
        assert_eq!(output, [255; 8]);
        assert_eq!(camera.discarded_frames(), Some(1));
        assert_eq!(camera.poll_frame(1, &mut output), Err(DeviceError::Io));
        assert_eq!(driver().dequeue_count, 1);
        camera.stop().unwrap();
    }

    // A failed immediate requeue reports and latches the driver error.
    {
        let mut camera = camera();
        start_jpeg(&mut camera);
        driver().queue_error_at_call = Some(3);
        let mut output = [0; 8];
        assert_eq!(camera.poll_frame(0, &mut output), Err(DeviceError::Io));
        assert_eq!(output, [1, 2, 3, 4, 5, 6, 7, 8]);
        assert_eq!(camera.fault, Some(DeviceError::Io));
        assert_eq!(camera.next_poll_at_ms(), None);
        assert_eq!(camera.poll_frame(1, &mut output), Err(DeviceError::Io));
        assert_eq!(driver().queue_count, 2);
        camera.stop().unwrap();
    }

    // Failure while queuing buffer two cleans up the partially prepared start.
    {
        let mut camera = camera();
        driver().queue_error_at_call = Some(2);
        assert_eq!(camera.start(JPEG_FORMAT), Err(DeviceError::Io));
        assert!(camera.file.is_none());
        assert!(camera.buffers.iter().all(Option::is_none));
        let state = driver();
        assert_eq!(state.queue_count, 1);
        assert_eq!(state.stream_on_count, 0);
        assert_eq!(state.close_count, 1);
        drop(state);
        assert_eq!(camera.start(JPEG_FORMAT), Ok(JPEG_FORMAT));
        camera.stop().unwrap();
    }

    // Even a failed STREAMON may have started hardware; cleanup attempts OFF.
    {
        let mut camera = camera();
        driver().on_error = -6;
        assert_eq!(camera.start(JPEG_FORMAT), Err(DeviceError::Io));
        assert!(camera.buffers.iter().all(Option::is_none));
        let state = driver();
        assert_eq!(state.stream_on_count, 1);
        assert_eq!(state.stream_off_count, 1);
        assert_eq!(state.close_count, 1);
    }

    // Failed quiescence retains the descriptor and both DMA allocations for retry.
    {
        let mut camera = camera();
        start_jpeg(&mut camera);
        driver().off_error = -6;
        assert_eq!(camera.stop(), Err(DeviceError::Io));
        assert!(camera.buffers.iter().all(Option::is_some));
        assert_eq!(driver().close_count, 0);
        driver().off_error = 0;
        camera.stop().unwrap();
        assert!(camera.buffers.iter().all(Option::is_none));
        assert_eq!(driver().close_count, 1);
    }

    // Preserve format errors and terminal close-error behavior.
    {
        let mut camera = camera();
        driver().format_error = -1;
        assert_eq!(camera.start(JPEG_FORMAT), Err(DeviceError::Unsupported));
        assert_eq!(driver().close_count, 1);
    }
    {
        let mut camera = camera();
        start_jpeg(&mut camera);
        driver().close_error = -6;
        assert_eq!(camera.stop(), Err(DeviceError::Io));
        assert_eq!(camera.stop(), Err(DeviceError::Io));
        assert_eq!(driver().close_count, 1);
        assert_eq!(camera.set_jpeg_quality(quality), Err(DeviceError::Io));
        assert_eq!(camera.jpeg_quality, None);
    }

    // Big-endian RGB565 is still normalized after copying the completed buffer.
    {
        let mut camera = camera();
        assert_eq!(camera.start(FORMAT), Ok(FORMAT));
        let mut output = [0; 8];
        camera.poll_frame(0, &mut output).unwrap().unwrap();
        assert_eq!(output, [2, 1, 4, 3, 6, 5, 8, 7]);
        camera.stop().unwrap();
    }
}
