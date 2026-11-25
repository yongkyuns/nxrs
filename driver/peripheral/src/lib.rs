//! Synchronous peripheral protocols shared by chip drivers and their consumers.
//! No resource construction, scheduling, profile selection or service policy.
#![forbid(unsafe_code)]
#![cfg_attr(target_os = "none", no_std)]

pub mod battery;
pub mod imu;

pub mod gnss;
