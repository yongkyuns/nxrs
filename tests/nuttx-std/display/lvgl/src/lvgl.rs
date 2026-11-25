//! Synchronous, single-owner adapter for the LVGL 9.2.2 qualification image.

use display::{Display, Rect, Size};
use std::ffi::{CStr, c_char, c_uint, c_void};
use std::ptr::{self, NonNull};
use std::slice;
use std::sync::Mutex;

const RGB565: c_uint = 0x12;
const PARTIAL: c_uint = 0;
const BUFFER_ROWS: u16 = 24;
static ENGINE: Mutex<()> = Mutex::new(());

#[repr(C)]
struct LvDisplay {
    _opaque: [u8; 0],
}

#[repr(C)]
struct LvObject {
    _opaque: [u8; 0],
}

#[repr(C)]
struct Area {
    x1: i32,
    y1: i32,
    x2: i32,
    y2: i32,
}

unsafe extern "C" {
    fn lv_is_initialized() -> bool;
    fn lv_init();
    fn lv_deinit();
    fn lv_display_create(width: i32, height: i32) -> *mut LvDisplay;
    fn lv_display_set_color_format(display: *mut LvDisplay, format: c_uint);
    fn lv_display_set_buffers(
        display: *mut LvDisplay,
        first: *mut c_void,
        second: *mut c_void,
        bytes: u32,
        mode: c_uint,
    );
    fn lv_display_set_user_data(display: *mut LvDisplay, data: *mut c_void);
    fn lv_display_get_user_data(display: *mut LvDisplay) -> *mut c_void;
    fn lv_display_set_flush_cb(
        display: *mut LvDisplay,
        flush: unsafe extern "C" fn(*mut LvDisplay, *const Area, *mut u8),
    );
    fn lv_display_flush_ready(display: *mut LvDisplay);
    fn lv_screen_active() -> *mut LvObject;
    fn lv_obj_set_style_bg_opa(object: *mut LvObject, opacity: u8, selector: u32);
    fn lv_label_create(parent: *mut LvObject) -> *mut LvObject;
    fn lv_label_set_text_static(label: *mut LvObject, text: *const c_char);
    fn lv_obj_center(object: *mut LvObject);
    fn lv_refr_now(display: *mut LvDisplay);
}

#[derive(Debug)]
pub enum Error {
    Busy,
    Initialization,
    InvalidFlush,
    Display(display::Error),
    IncompleteFrame,
}

struct Engine;

impl Drop for Engine {
    fn drop(&mut self) {
        // SAFETY: this invocation initialized the only engine and holds its lock.
        unsafe { lv_deinit() };
    }
}

struct FlushTarget<'a> {
    screen: &'a mut dyn Display,
    buffer: *mut u8,
    capacity_pixels: usize,
    pixels: usize,
    chunks: usize,
    error: Option<Error>,
}

pub fn render_label(screen: &mut dyn Display, text: &'static CStr) -> Result<usize, Error> {
    let _lock = ENGINE.try_lock().map_err(|_| Error::Busy)?;
    // SAFETY: all engine calls are serialized by ENGINE.
    if unsafe { lv_is_initialized() } {
        return Err(Error::Busy);
    }

    let size = screen.size();
    if size.width == 0 || size.height == 0 {
        return Err(Error::Initialization);
    }
    let pixels = usize::from(size.width) * usize::from(size.height.min(BUFFER_ROWS));
    // u32 storage supplies LVGL's 4-byte alignment without a full framebuffer.
    let mut buffer = vec![0_u32; pixels.div_ceil(2)];
    let mut target = FlushTarget {
        screen,
        buffer: buffer.as_mut_ptr().cast(),
        capacity_pixels: pixels,
        pixels: 0,
        chunks: 0,
        error: None,
    };

    // SAFETY: NuttX compiles the pinned LVGL 9.2.2 API with one synchronous
    // draw unit. The context and buffer remain live until after Engine drops.
    unsafe {
        lv_init();
        let _engine = Engine;
        let display = NonNull::new(lv_display_create(size.width.into(), size.height.into()))
            .ok_or(Error::Initialization)?;
        lv_display_set_color_format(display.as_ptr(), RGB565);
        lv_display_set_buffers(
            display.as_ptr(),
            target.buffer.cast(),
            ptr::null_mut(),
            (pixels * size_of::<u16>()) as u32,
            PARTIAL,
        );
        lv_display_set_user_data(display.as_ptr(), ptr::from_mut(&mut target).cast());
        lv_display_set_flush_cb(display.as_ptr(), flush);

        let root = NonNull::new(lv_screen_active()).ok_or(Error::Initialization)?;
        // The minimal configuration has no theme to paint the screen background.
        lv_obj_set_style_bg_opa(root.as_ptr(), u8::MAX, 0);
        let label = NonNull::new(lv_label_create(root.as_ptr())).ok_or(Error::Initialization)?;
        lv_label_set_text_static(label.as_ptr(), text.as_ptr());
        lv_obj_center(label.as_ptr());
        lv_refr_now(display.as_ptr());
    }

    if let Some(error) = target.error {
        return Err(error);
    }
    if target.pixels != usize::from(size.width) * usize::from(size.height) {
        return Err(Error::IncompleteFrame);
    }
    Ok(target.chunks)
}

unsafe extern "C" fn flush(display: *mut LvDisplay, area: *const Area, pixels: *mut u8) {
    // SAFETY: LVGL passes its live displayed area and the context registered for
    // this synchronous refresh. No other code accesses the context during it.
    let target = unsafe { &mut *lv_display_get_user_data(display).cast::<FlushTarget<'_>>() };
    if target.error.is_none() {
        let area = unsafe { &*area };
        let result = target.draw(area, pixels);
        if let Err(error) = result {
            target.error = Some(error);
        }
    }
    // SAFETY: the blocking draw has returned and no borrow of its pixels remains.
    // Always acknowledge the flush so LVGL can finish even after a display error.
    unsafe { lv_display_flush_ready(display) };
}

impl FlushTarget<'_> {
    fn draw(&mut self, area: &Area, pixels: *mut u8) -> Result<(), Error> {
        let [Ok(x1), Ok(y1), Ok(x2), Ok(y2)] =
            [area.x1, area.y1, area.x2, area.y2].map(u16::try_from)
        else {
            return Err(Error::InvalidFlush);
        };
        let screen = self.screen.size();
        if x1 > x2 || y1 > y2 || x2 >= screen.width || y2 >= screen.height {
            return Err(Error::InvalidFlush);
        }
        let size = Size {
            width: x2 - x1 + 1,
            height: y2 - y1 + 1,
        };
        let count = usize::from(size.width) * usize::from(size.height);
        if pixels != self.buffer || count > self.capacity_pixels {
            return Err(Error::InvalidFlush);
        }
        // SAFETY: this is the aligned, initialized RGB565 buffer installed by
        // render_label. With PARTIAL mode and stride alignment 1 it is packed.
        // LVGL resumes writing only after lv_display_flush_ready.
        let words = unsafe { slice::from_raw_parts(self.buffer.cast::<u16>(), count) };
        self.screen
            .draw(Rect { x: x1, y: y1, size }, words)
            .map_err(Error::Display)?;
        self.pixels += count;
        self.chunks += 1;
        Ok(())
    }
}
