//! Synchronous presentation over the portable display and touch capabilities.
//!
//! The caller owns its scenes, colors, deadlines and event loop. [`Surface`]
//! owns one reusable pixel strip; chart and history helpers keep bounded state.
//! No component starts a worker or opens a device.
#![forbid(unsafe_code)]

pub mod chart;
mod font;
pub mod gesture;
mod history;
mod surface;
mod text;

pub use history::History;
pub use surface::Surface;
pub use text::TextCanvas;
