//! White-box measurements of the existing private bounded Vec recipe.
use super::BoundedVec;
use nxrs_allocation_probe::{Snapshot, Tracking, begin_measurement};
use std::alloc::System;
use std::collections::HashMap;
use std::fmt::Write as _;
use std::hint::black_box;

#[global_allocator]
static ALLOCATOR: Tracking<System> = Tracking::new(System);

fn report(case: &str, samples: [Snapshot; 5]) {
    let [before, initialized, first, steady, released] = samples;
    first
        .no_allocator_calls_since(initialized)
        .unwrap_or_else(|error| {
            panic!("{case} first-use: {error}; initialized={initialized:?}; first={first:?}")
        });
    steady
        .no_allocator_calls_since(first)
        .unwrap_or_else(|error| {
            panic!("{case} steady: {error}; first={first:?}; steady={steady:?}")
        });
    released.reclaimed_since(before).unwrap();
    initialized.report(before, "std-demo", case, "setup");
    first.report(initialized, "std-demo", case, "first-use");
    steady.report(first, "std-demo", case, "steady");
    released.report(before, "std-demo", case, "reclaimed");
}

fn bounded_vec() {
    let before = ALLOCATOR.snapshot();
    let mut values = BoundedVec::<u32>::try_new(16).unwrap();
    let initialized = ALLOCATOR.snapshot();
    for value in 0..16 {
        values.try_push(value).unwrap();
    }
    black_box(&values.values);
    values.clear();
    let first = ALLOCATOR.snapshot();
    for cycle in 0..1000 {
        for value in 0..16 {
            values.try_push(cycle + value).unwrap();
        }
        assert_eq!(values.try_push(999), Err(999));
        black_box(&values.values);
        values.clear();
    }
    let steady = ALLOCATOR.snapshot();
    drop(values);
    let released = ALLOCATOR.snapshot();
    report(
        "vec-bounded",
        [before, initialized, first, steady, released],
    );
}

fn maps() {
    let before = ALLOCATOR.snapshot();
    let mut values = HashMap::<&'static str, u32>::new();
    values.try_reserve(3).unwrap();
    let initialized = ALLOCATOR.snapshot();
    for key in ["imu", "gnss", "camera"] {
        values.insert(key, 0);
    }
    black_box(&values);
    let first = ALLOCATOR.snapshot();
    for _ in 0..1000 {
        for key in ["imu", "gnss", "camera"] {
            *values.entry(key).or_insert(0) += 1;
        }
        black_box(&values);
    }
    let steady = ALLOCATOR.snapshot();
    drop(values);
    let released = ALLOCATOR.snapshot();
    report("hashmap", [before, initialized, first, steady, released]);
}

fn text() {
    let before = ALLOCATOR.snapshot();
    let mut value = String::new();
    value.try_reserve(32).unwrap();
    let initialized = ALLOCATOR.snapshot();
    write!(&mut value, "sample={}", black_box(7u16)).unwrap();
    black_box(&value);
    value.clear();
    let first = ALLOCATOR.snapshot();
    for sequence in 0..1000u16 {
        write!(&mut value, "sample={sequence}").unwrap();
        black_box(&value);
        value.clear();
    }
    let steady = ALLOCATOR.snapshot();
    drop(value);
    let released = ALLOCATOR.snapshot();
    report("string", [before, initialized, first, steady, released]);
}

fn inline_array() {
    let before = ALLOCATOR.snapshot();
    let mut values = [0u32; 16];
    for cycle in 0..1000 {
        for value in &mut values {
            *value += cycle;
        }
        black_box(&mut values);
    }
    let after = ALLOCATOR.snapshot();
    after.no_allocator_calls_since(before).unwrap();
    after.reclaimed_since(before).unwrap();
    after.report(before, "std-demo", "fixed-array", "steady");
}

fn dynamic_growth() {
    let before = ALLOCATOR.snapshot();
    let mut values = Vec::<u32>::new();
    for value in 0..256 {
        values.push(value);
        black_box(&values);
    }
    let grown = ALLOCATOR.snapshot();
    assert!(grown.alloc > before.alloc);
    assert!(grown.realloc > before.realloc);
    assert!(grown.live > before.live);
    drop(values);
    let released = ALLOCATOR.snapshot();
    released.reclaimed_since(before).unwrap();
    grown.report(before, "std-demo", "vec-dynamic", "growth");
    released.report(before, "std-demo", "vec-dynamic", "reclaimed");
}

fn reservation_is_not_a_limit() {
    let before = ALLOCATOR.snapshot();
    let mut values = Vec::<u16>::with_capacity(16);
    let initialized = ALLOCATOR.snapshot();
    values.resize(values.capacity(), 7);
    black_box(&values);
    let filled = ALLOCATOR.snapshot();
    values.push(8);
    black_box(&values);
    let grown = ALLOCATOR.snapshot();
    drop(values);
    let released = ALLOCATOR.snapshot();
    filled.no_allocator_calls_since(initialized).unwrap();
    assert!(grown.realloc > filled.realloc);
    released.reclaimed_since(before).unwrap();
    initialized.report(before, "std-demo", "vec-reserved", "setup");
    filled.report(initialized, "std-demo", "vec-reserved", "reserved-fill");
    grown.report(filled, "std-demo", "vec-reserved", "growth");
    released.report(before, "std-demo", "vec-reserved", "reclaimed");
}

fn observer_negative_control() {
    let before = ALLOCATOR.snapshot();
    let mut unexpected = Vec::<u8>::with_capacity(512);
    unexpected.resize(512, 42);
    black_box(&unexpected);
    let held = ALLOCATOR.snapshot();
    assert!(held.no_allocator_calls_since(before).is_err());
    assert!(held.reclaimed_since(before).is_err());
    drop(unexpected);
    ALLOCATOR.snapshot().reclaimed_since(before).unwrap();
    println!("MEMORY_CONTROL app=std-demo kind=allocation-and-retention rejected=true");
}

pub(crate) fn run() {
    begin_measurement();
    observer_negative_control();
    dynamic_growth();
    reservation_is_not_a_limit();
    bounded_vec();
    maps();
    text();
    inline_array();
    println!("MEMORY_PASS app=std-demo cases=6");
}
