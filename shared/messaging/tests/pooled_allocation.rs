//! One isolated process checks requested pool allocations, not OS heap usage.
#![forbid(unsafe_code)]

use allocation_probe::{Snapshot, Tracking};
use messaging::pooled::{
    channel, required_bytes, storage_report, Notify, Record, RetainedBudget, SetupError, Status,
};
use std::alloc::System;

#[global_allocator]
static ALLOCATOR: Tracking<System> = Tracking::new(System);

struct NoWake;
impl Notify for NoWake {
    fn notify(&self) {}
}

fn snapshot() -> Snapshot {
    ALLOCATOR.snapshot()
}

fn main() {
    let required = required_bytes::<NoWake>();
    let before = snapshot();
    let rejected = channel(
        RetainedBudget {
            max_requested_bytes: required - 1,
        },
        NoWake,
    );
    let after = snapshot();
    after.no_allocator_calls_since(before).unwrap();
    assert!(matches!(
        rejected,
        Err(SetupError::BudgetExceeded { required: actual, maximum })
            if actual == required && maximum == required - 1
    ));

    let before = snapshot();
    let (mut owner, [mut source, secondary, gnss]) = channel(
        RetainedBudget {
            max_requested_bytes: required,
        },
        NoWake,
    )
    .unwrap();
    let allocated = snapshot();
    allocated.validate_after(before).unwrap();
    assert_eq!(allocated.alloc - before.alloc, 5);
    assert_eq!(allocated.zeroed - before.zeroed, 0);
    assert_eq!(allocated.realloc - before.realloc, 0);
    assert_eq!(allocated.dealloc - before.dealloc, 0);
    assert_eq!(allocated.blocks - before.blocks, 5);
    assert_eq!(allocated.live - before.live, required);
    assert_eq!(storage_report::<NoWake>().requested_heap_bytes, required);

    let epoch = owner.begin().unwrap();
    for sequence in 0..4096 {
        let record = Record {
            sequence,
            words: [sequence ^ 0xa15e_4460; 8],
        };
        source.offer(epoch, record).unwrap();
        assert_eq!(owner.poll().unwrap().record, record);
    }
    owner.cancel();
    assert_eq!(owner.finish(), Ok(Status::Cancelled));
    let inactive = snapshot();
    inactive.no_allocator_calls_since(allocated).unwrap();
    assert_eq!(inactive.live - before.live, required);
    assert_eq!(owner.high_water(), 1);

    drop(owner);
    drop(secondary);
    drop(gnss);
    // A surviving inactive publisher keeps the same arena without freeing it
    // on the acquisition path. Final teardown is the explicit release point.
    snapshot().no_allocator_calls_since(inactive).unwrap();
    drop(source);
    let reclaimed = snapshot();
    reclaimed.reclaimed_since(before).unwrap();
    assert_eq!(reclaimed.dealloc - before.dealloc, 5);
    println!("POOLED_ALLOCATION PASS requested_bytes={required} allocations=5 steady_roundtrips=4096 inactive_retained=true");
}
