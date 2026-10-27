//! Ordinary Rust std entry for the full NuttX integration qualification.
//!
//! This is a target test fixture, not production application code. The shared
//! qualification modules deliberately cross documented C FFI boundaries for
//! synthetic devices, fault injection, and ABI witnesses. Keep unsafe
//! operations explicit rather than forbidding those test-only boundaries.
#![deny(unsafe_op_in_unsafe_fn)]

mod app;
mod qualification;
#[path = "qualification/std_storage.rs"]
mod std_storage;

use std::ffi::CString;
use std::thread;
use std::time::{Duration, Instant};

fn main() {
    println!("RC_RUST_STD_MAIN BEGIN");

    std_storage::run();

    let args: Vec<String> = std::env::args().skip(1).collect();
    assert_eq!(args.len(), 2, "usage: nxrs <output> <udp-port>");
    let output = CString::new(args[0].as_bytes()).expect("output path contains NUL");
    let port: u16 = args[1].parse().expect("invalid UDP port");
    let transport =
        transport::UdpSender::connect([127, 0, 0, 1], port)
            .expect("NuttX std UDP connect");

    let origin = Instant::now();
    let now_ms = || u64::try_from(origin.elapsed().as_millis()).unwrap_or(u64::MAX);
    let sleep_ms = |milliseconds| thread::sleep(Duration::from_millis(milliseconds));

    assert_eq!(app::run(output.as_c_str(), transport, now_ms, sleep_ms), 0);
    println!("RC_RUST_STD_MAIN PASS");
}
