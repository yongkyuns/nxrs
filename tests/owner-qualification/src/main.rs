//! Current-source finite execution witnesses. No production transport changes.
#![deny(unsafe_op_in_unsafe_fn)]

mod platform;
mod scenarios;

use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};

pub const CASES: [&str; 7] = [
    "std-blocked-delivery",
    "std-full-progress",
    "std-disconnect",
    "owner-independent-inputs",
    "owner-full-stop",
    "owner-fairness-deadline",
    "owner-failure-recovery",
];
static USED: AtomicBool = AtomicBool::new(false);

#[derive(Default, Serialize)]
pub struct Outcome {
    participants: Vec<platform::Participant>,
    attempted: usize,
    admitted: usize,
    rejected: usize,
    handled: usize,
    discarded: usize,
    joined: usize,
    required_waits: usize,
    native_waits: usize,
    important_observations: usize,
    ordinary_observations: usize,
    fairness_links_before_ordinary: usize,
    deadline_observations: usize,
    failure_observations: usize,
    stop_requests: usize,
    endpoints_closed: bool,
}

fn main() {
    assert!(
        !USED.swap(true, Ordering::SeqCst),
        "one invocation per fresh boot"
    );
    let args: Vec<_> = std::env::args().skip(1).collect();
    assert_eq!(args.len(), 2, "usage: owner_qualification CASE RUN_TOKEN");
    let case = &args[0];
    let token = &args[1];
    assert!(CASES.contains(&case.as_str()), "unknown qualification case");
    assert!(
        (8..=64).contains(&token.len())
            && token
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-'),
        "invalid run token"
    );
    let id = env!("OWNER_QUALIFICATION_BUILD_ID");
    println!("OWNER_QUALIFICATION_BEGIN {id} {case} {token}");
    platform::watchdog(true);
    let priority = if case.starts_with("owner-") { 180 } else { 220 };
    let coordinator = platform::enter("coordinator", priority);
    let mut result = scenarios::run(case);
    result.participants.push(platform::leave(coordinator));
    platform::watchdog(false);
    assert_eq!(result.attempted, result.admitted + result.rejected);
    assert_eq!(result.admitted, result.handled + result.discarded);
    assert!(result.endpoints_closed);
    assert_eq!(
        result.native_waits,
        if platform::NATIVE {
            result.required_waits
        } else {
            0
        }
    );
    let report = serde_json::json!({
        "schema":"owner-qualification-v1", "case":case, "token":token,
        "build_id":id, "domain":if platform::NATIVE {"nuttx-target"} else {"host-functional"},
        "native_scheduling":platform::NATIVE, "fifo_policy":platform::fifo_policy(), "physical_execution_verified":false,
        "timing_qualified":false, "production_qualified":false,
        "quiescence_basis":"all-fixture-workers-joined",
        "pass":true, "observations":result
    });
    println!("OWNER_QUALIFICATION_RESULT {report}");
    println!("OWNER_QUALIFICATION_END {id} {case} {token}");
}
