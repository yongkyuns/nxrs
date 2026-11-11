//! Synchronous peripheral protocols shared by chip drivers and their consumers.
//! No resource construction, scheduling, profile selection or service policy.
#![forbid(unsafe_code)]

pub mod imu;

pub mod gnss;
