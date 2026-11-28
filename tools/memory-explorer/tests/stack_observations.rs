use nxrs_memory_explorer::*;
use serde_json::{json, Value};

fn capture() -> Value {
    json!({
        "schema_version": 1,
        "build": {"elf_sha256": "a".repeat(64)},
        "regions": [{"id":"ram", "name":"RAM", "start":4096, "size":4096, "kind":"ram"}],
        "snapshots": [{
            "time_ns": 10, "label": "stopped", "complete_regions":["ram"],
            "spans": [
                {"id":"heap", "name":"heap", "region":"ram", "start":4096, "size":4096, "kind":"heap_arena"},
                {"id":"chunk", "name":"allocated chunk", "region":"ram", "start":4096, "size":1024, "kind":"allocation", "parent":"heap"},
                {"id":"stack", "name":"task 7", "region":"ram", "start":4112, "size":1008, "kind":"stack", "parent":"chunk"},
                {"id":"free", "name":"free", "region":"ram", "start":5120, "size":3072, "kind":"free", "parent":"heap"}
            ],
            "stack_observations": [{
                "stack":"stack", "task_id":7, "tcb":9000, "growth":"down",
                "current":{"status":"observed", "sp":4800, "source":"saved_context"},
                "high_water":{"status":"coloration", "scan_start":4112, "scan_size":1008,
                              "used_bytes":512, "pattern":3735928559_u32, "word_bytes":4}
            }]
        }]
    })
}

fn model(value: &Value) -> MemoryModel {
    MemoryModel::from_snapshot(parse_snapshot(&value.to_string()).unwrap()).unwrap()
}

#[test]
fn pointer_depth_coloration_and_reservation_remain_distinct_and_nonadditive() {
    let model = model(&capture());
    let snapshot = model.snapshot_at(10).unwrap();
    let stack = snapshot
        .spans
        .iter()
        .find(|span| span.id == "stack")
        .unwrap();
    let observation = &snapshot.stack_observations[0];
    assert_eq!(stack.size, 1008);
    assert_eq!(observation.current_used(stack), Some(320));
    assert_eq!(observation.current_headroom(stack), Some(688));
    assert_eq!(observation.watermark_used(), Some(512));
    assert_eq!(observation.watermark_headroom(), Some(496));
    assert_eq!(
        observation.current_range(stack).unwrap(),
        AddressRange {
            start: 4800,
            end: 5120
        }
    );
    assert_eq!(
        observation.watermark_range().unwrap(),
        AddressRange {
            start: 4608,
            end: 5120
        }
    );
    assert_eq!(model.occupied_bytes("ram", 10), 1024);
    assert_eq!(model.free_bytes("ram", 10), 3072);
    assert_eq!(
        model.heap_statistics("ram", 10)[0].allocated_chunk_bytes,
        1024
    );
    assert!(model.snapshot_at(11).is_none());
    assert_eq!(model.occupied_bytes("ram", 11), 0);
}

#[test]
fn historical_reservations_load_without_inventing_stack_measurements() {
    let mut value = capture();
    value["snapshots"][0]
        .as_object_mut()
        .unwrap()
        .remove("stack_observations");
    let model = model(&value);
    assert!(model.snapshots[0].stack_observations.is_empty());
    assert!(model.snapshots[0]
        .spans
        .iter()
        .any(|span| span.kind == SpanKind::Stack));
}

#[test]
fn disabled_coloration_and_missing_context_preserve_separate_reasons() {
    let mut value = capture();
    value["snapshots"][0]["stack_observations"][0]["current"] =
        json!({"status":"unavailable", "reason":"stopped in interrupt context"});
    value["snapshots"][0]["stack_observations"][0]["high_water"] =
        json!({"status":"unavailable", "reason":"stack coloration disabled"});
    let model = model(&value);
    let observation = &model.snapshots[0].stack_observations[0];
    assert_eq!(observation.current_used(&model.snapshots[0].spans[2]), None);
    assert_eq!(observation.watermark_headroom(), None);
    assert!(
        matches!(&observation.current, StackCurrent::Unavailable { reason } if reason.contains("interrupt"))
    );
    assert!(
        matches!(&observation.high_water, StackHighWater::Unavailable { reason } if reason.contains("disabled"))
    );
}

#[test]
fn current_pointer_can_reserve_more_space_than_color_scan_shows_touched() {
    let mut value = capture();
    value["snapshots"][0]["stack_observations"][0]["current"]["sp"] = json!(4200);
    let model = model(&value);
    let observation = &model.snapshots[0].stack_observations[0];
    assert_eq!(
        observation.current_used(&model.snapshots[0].spans[2]),
        Some(920)
    );
    assert_eq!(observation.watermark_used(), Some(512));
}

#[test]
fn all_colored_zero_and_fully_touched_stack_are_valid_evidence() {
    for (used, spare, range) in [
        (0, 1008, None),
        (
            1008,
            0,
            Some(AddressRange {
                start: 4112,
                end: 5120,
            }),
        ),
    ] {
        let mut value = capture();
        value["snapshots"][0]["stack_observations"][0]["high_water"]["used_bytes"] = json!(used);
        let model = model(&value);
        let observation = &model.snapshots[0].stack_observations[0];
        assert_eq!(observation.watermark_used(), Some(used));
        assert_eq!(observation.watermark_headroom(), Some(spare));
        assert_eq!(observation.watermark_range(), range);
    }
}

#[test]
fn unscanned_alignment_edges_do_not_inflate_untouched_headroom() {
    let mut value = capture();
    let scan = &mut value["snapshots"][0]["stack_observations"][0]["high_water"];
    scan["scan_start"] = json!(4120);
    scan["scan_size"] = json!(992);
    let model = model(&value);
    let observation = &model.snapshots[0].stack_observations[0];
    assert_eq!(observation.watermark_headroom(), Some(480));
    assert_eq!(
        observation.watermark_range(),
        Some(AddressRange {
            start: 4600,
            end: 5112
        })
    );
    assert_eq!(
        observation.current_headroom(&model.snapshots[0].spans[2]),
        Some(688)
    );
}

#[test]
fn upward_stacks_preserve_exact_ranges() {
    let mut value = capture();
    value["snapshots"][0]["stack_observations"][0]["growth"] = json!("up");
    let model = model(&value);
    let observation = &model.snapshots[0].stack_observations[0];
    assert_eq!(
        observation.current_used(&model.snapshots[0].spans[2]),
        Some(688)
    );
    assert_eq!(
        observation.current_range(&model.snapshots[0].spans[2]),
        Some(AddressRange {
            start: 4112,
            end: 4800
        })
    );
    assert_eq!(
        observation.watermark_range(),
        Some(AddressRange {
            start: 4112,
            end: 4624
        })
    );
}

#[test]
fn pointer_at_each_boundary_is_observed_zero_or_full_depth() {
    for (sp, used, headroom) in [(5120, 0, 1008), (4112, 1008, 0)] {
        let mut value = capture();
        value["snapshots"][0]["stack_observations"][0]["current"]["sp"] = json!(sp);
        let model = model(&value);
        let observation = &model.snapshots[0].stack_observations[0];
        let stack = &model.snapshots[0].spans[2];
        assert_eq!(observation.current_used(stack), Some(used));
        assert_eq!(observation.current_headroom(stack), Some(headroom));
        assert_eq!(observation.current_range(stack).is_none(), used == 0);
    }
}

#[test]
fn contradictory_or_ambiguous_observations_are_rejected() {
    let cases = [
        ("/stack", json!("absent")),
        ("/stack", json!("chunk")),
        ("/tcb", json!(0)),
        ("/task_id", json!(-1)),
        ("/growth", json!("unknown")),
        ("/current/sp", json!(4111)),
        ("/current/sp", json!(5121)),
        ("/current/sp", json!(4800.0)),
        ("/current", json!({"status":"unavailable","reason":"  "})),
        ("/current", json!({"status":"observed","sp":4800})),
        ("/high_water/scan_start", json!(4108)),
        ("/high_water/scan_start", json!(4113)),
        ("/high_water/scan_size", json!(0)),
        ("/high_water/scan_size", json!(1012)),
        ("/high_water/scan_size", json!(u64::MAX)),
        ("/high_water/used_bytes", json!(1012)),
        ("/high_water/used_bytes", json!(511)),
        ("/high_water/word_bytes", json!(8)),
        ("/high_water/pattern", json!(1_u64 << 32)),
        ("/high_water", json!({"status":"unavailable","reason":""})),
    ];
    for (pointer, replacement) in cases {
        let mut value = capture();
        *value["snapshots"][0]["stack_observations"][0]
            .pointer_mut(pointer)
            .unwrap() = replacement;
        assert!(
            parse_snapshot(&value.to_string()).is_err(),
            "accepted invalid {pointer}: {value}"
        );
    }
    let mut value = capture();
    let duplicate = value["snapshots"][0]["stack_observations"][0].clone();
    value["snapshots"][0]["stack_observations"]
        .as_array_mut()
        .unwrap()
        .push(duplicate);
    assert!(parse_snapshot(&value.to_string()).is_err());
    let mut value = capture();
    value["snapshots"][0]["stack_observations"][0]["lifetime"] = json!("invented");
    assert!(parse_snapshot(&value.to_string()).is_err());
}

#[test]
fn stack_identity_is_scoped_to_one_checkpoint_and_survives_roundtrip() {
    let mut value = capture();
    let mut next = value["snapshots"][0].clone();
    next["time_ns"] = json!(20);
    next["stack_observations"][0]["task_id"] = json!(8);
    next["stack_observations"][0]["high_water"]["used_bytes"] = json!(0);
    value["snapshots"].as_array_mut().unwrap().push(next);
    let file = parse_snapshot(&value.to_string()).unwrap();
    let roundtrip = parse_snapshot(&serde_json::to_string(&file).unwrap()).unwrap();
    assert_eq!(
        roundtrip.snapshots[0].stack_observations,
        file.snapshots[0].stack_observations
    );
    assert_eq!(
        roundtrip.snapshots[1].stack_observations[0].watermark_used(),
        Some(0)
    );
}
