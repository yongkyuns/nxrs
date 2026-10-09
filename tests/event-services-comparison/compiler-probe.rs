//! Diagnostic wrapper around the unchanged service workload, not app policy.
#![no_std]
#![allow(dead_code)]

#[path = "core.rs"]
mod contract;

#[no_mangle]
pub extern "C" fn probe_rust(value: u32, token: u32, iterations: u32) -> u32 {
    contract::work_value(value, token, iterations)
}
