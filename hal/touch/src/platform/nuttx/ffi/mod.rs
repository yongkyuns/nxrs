use std::ffi::c_int;

#[repr(C)]
pub(super) struct Layout {
    pub nonblock: c_int,
    pub size: u8,
    pub point: u8,
    pub id: u8,
    pub flags: u8,
    pub x: u8,
    pub y: u8,
    pub down: u8,
    pub up: u8,
    pub movement: u8,
    pub position: u8,
}

unsafe extern "C" {
    fn touch_record_layout() -> Layout;
}

pub(super) fn layout() -> Layout {
    // The C witness provides target-header offsets for one fixed-size sample.
    unsafe { touch_record_layout() }
}
