//! Single-contact touch events in display coordinates.
#![deny(unsafe_code)]

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Point {
    pub x: u16,
    pub y: u16,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Event {
    Pressed(Point),
    Moved(Point),
    Released,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    Unsupported,
    Busy,
    Io,
    InvalidReport,
}

pub trait Session {
    fn is_finished(&self) -> bool;
    fn request_stop(&mut self) -> Result<(), Error>;
    fn join(&mut self) -> Result<(), Error>;
}

mod platform;

/// Open the selected touchscreen. A sink returning false stops acquisition.
/// The sink runs in task context and should return promptly.
pub use platform::open;
