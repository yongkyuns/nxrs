//! Bounded raster tiles, incremental transport framing and a north-up map view.
//!
//! The caller supplies progress and owns transport and rendering. [`wire::Receiver`]
//! decodes the same tile protocol from any [`std::io::Read`] source. [`View`]
//! retains at most four completed tiles and selects the next visible tile to fetch.
//! No worker, event queue, device selection or network connection is created.
#![forbid(unsafe_code)]

mod viewport;
pub mod wire;

pub use viewport::{Position, View, Viewport};
pub use wire::{Key, Tile, Zoom, SIDE};
