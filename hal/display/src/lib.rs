//! Synchronous RGB565 output to a profile-selected display.
#![deny(unsafe_code)]

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Size {
    pub width: u16,
    pub height: u16,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Rect {
    pub x: u16,
    pub y: u16,
    pub size: Size,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    Unsupported,
    Busy,
    InvalidRegion,
    InvalidPixelCount,
    Io,
}

/// Exclusive display owner within Nxrs. Dropping it leaves the last image visible.
pub trait Display {
    fn size(&self) -> Size;

    /// Draw a packed, nonempty rectangle of row-major RGB565 words.
    ///
    /// The pixel count must exactly match the area. The transfer completes
    /// before returning. An I/O failure may leave a partial update.
    fn draw(&mut self, area: Rect, pixels: &[u16]) -> Result<(), Error>;
}

mod platform;
pub use platform::open;
