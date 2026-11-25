unsafe extern "C" {
    pub(super) fn camera_open_flags() -> i32;
    pub(super) fn camera_format(
        fd: i32,
        w: u16,
        h: u16,
        pixels: u8,
        actual_w: *mut u32,
        actual_h: *mut u32,
        actual_pixels: *mut u8,
    ) -> i32;
    pub(super) fn camera_quality(fd: i32, quality: u8) -> i32;
    pub(super) fn camera_buffers(fd: i32, count: u32) -> i32;
    pub(super) fn camera_queue(fd: i32, index: u32, data: *mut u8, length: usize) -> i32;
    pub(super) fn camera_dequeue(
        fd: i32,
        index: *mut u32,
        address: *mut usize,
        capacity: *mut u32,
        bytes: *mut u32,
        flags: *mut u32,
    ) -> i32;
    pub(super) fn camera_stream(fd: i32, on: i32) -> i32;
    pub(super) fn rc_nx_close(fd: i32) -> i32;
}
