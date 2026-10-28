//! Experimental CSI qualification core shared by live capture and virtual-time replay.
#![forbid(unsafe_code)]
pub mod calibration;
pub mod journal;
pub mod motion;
pub mod motion_pipeline;
pub mod motion_trial;
pub mod motion_wire;
pub mod ownership;
pub mod progress;
pub mod recorder;
pub mod sampling;
pub mod temporal;
