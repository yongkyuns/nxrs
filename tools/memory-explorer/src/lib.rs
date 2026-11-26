//! Host-side, rendering-independent firmware memory observations.
//!
//! Addresses are half-open integer intervals. CPU addresses, ELF file offsets,
//! and physical backing offsets are distinct domains. A snapshot describes one
//! observed instant; it never implies allocation history between observations.

mod demo;
mod import;
mod model;
mod timing;

pub use demo::demo;
pub use import::{load, load_inputs, parse_gnu_map, MapContribution};
pub use model::*;
pub use timing::{
    load_timing, parse_timing, timing_demo, CounterSample, TimingEvent, TimingEventKind,
    TimingMetadata, TimingRange, TimingStats, TimingTrace, TimingTrack, TimingTrackStats,
};
