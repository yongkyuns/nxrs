//! Compile-time experiment selection, independent of Cargo features.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Wire,
    Packet,
    Baseline,
}
include!(concat!(env!("OUT_DIR"), "/embassy_case.rs"));
