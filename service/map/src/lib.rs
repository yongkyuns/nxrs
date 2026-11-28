//! Bounded NXM5 raster tiles, incremental framing and a compact north-up map view.
//!
//! The caller supplies progress and owns transport and rendering. [`wire::Receiver`]
//! decodes the same tile protocol from any [`std::io::Read`] source. [`View`]
//! retains at most four completed tiles and selects the next visible tile to fetch.
//! The format has 170-pixel indexed tiles at zoom levels 15 through 18; the view
//! covers a one-tile-wide square at any output pixel size. Callers choose fetch
//! direction, progress and presentation policy.
//! No worker, event queue, device selection or network connection is created.
#![forbid(unsafe_code)]

mod viewport;
pub mod wire;

pub use viewport::{Direction, Position, View, Viewport};
pub use wire::{Key, Tile, Zoom, SIDE};
