use nxrs_memory_explorer::{
    parse_heap_journal, parse_snapshot, parse_timing, AddressDomain, HeapEvidence, HeapReplay,
    HeapState, HeapUnknownReason, MemoryModel, ReplayCoverageKind, ReplayCursor, SpanKind,
};
use serde_json::{json, Value};

fn memory() -> MemoryModel {
    MemoryModel::from_snapshot(parse_snapshot(&json!({
        "schema_version":1, "build":{"elf_sha256":"a".repeat(64)},
        "regions":[{"id":"ram", "name":"RAM", "start":4096, "size":4096, "kind":"ram"}],
        "snapshots":[{"time_ns":250,"label":"independent stack observation", "spans":[
            {"id":"stack","name":"Another task", "region":"ram", "start":5000, "size":256, "kind":"stack"}
        ]}]
    }).to_string()).unwrap()).unwrap()
}

fn chunk(start: u64, size: u64, state: &str) -> Value {
    json!({"start":start,"size":size,"state":state})
}

fn baseline() -> Value {
    json!([
        chunk(4096, 16, "reserved"),
        chunk(4112, 224, "free"),
        chunk(4336, 16, "reserved")
    ])
}

fn transaction(
    sequence: u64,
    start_ns: u64,
    start: u64,
    size: u64,
    expected: Vec<Value>,
    replacement: Vec<Value>,
) -> Value {
    json!({"kind":"transaction","sequence":sequence,"heap_epoch":"heap-epoch-1", "start_ns":start_ns,"commit_ns":start_ns+20,
        "start":start,"size":size,"expected":expected,"replacement":replacement,"label":format!("operation {sequence}")})
}

fn journal() -> Value {
    json!({
        "schema_version":1, "build":{"elf_sha256":"a".repeat(64),"label":"synthetic contract witness"},
        "provenance":{"nuttx_revision":"2b5c0de5e933281c209f954a63b99cf10c132346","config_sha256":"b".repeat(64),"collector":"synthetic-contract-witness"},
        "recording_id":"recording-1","clock":"target-clock",
        "heap":{"id":"fixture-heap","epoch":"heap-epoch-1","region":"ram","start":4096,"size":256},
        "initial":{"sequence":0,"time_ns":100,"chunks":baseline()},
        "records":[
            transaction(1,200,4112,224,vec![chunk(4112,224,"free")],vec![chunk(4112,48,"allocated"),chunk(4160,176,"free")]),
            transaction(2,300,4160,176,vec![chunk(4160,176,"free")],vec![chunk(4160,80,"allocated"),chunk(4240,96,"free")]),
            transaction(3,400,4112,48,vec![chunk(4112,48,"allocated")],vec![chunk(4112,48,"free")]),
            transaction(4,500,4112,224,vec![chunk(4112,48,"free"),chunk(4160,80,"allocated"),chunk(4240,96,"free")],vec![chunk(4112,224,"free")])
        ],
        "closing":{"heap_epoch":"heap-epoch-1","sequence":4,"time_ns":600}
    })
}

fn load(value: &Value) -> HeapReplay {
    parse_heap_journal(&value.to_string(), &memory(), None).unwrap()
}

fn known(replay: &HeapReplay, cursor: ReplayCursor) -> nxrs_memory_explorer::KnownHeapState<'_> {
    match replay.heap_state_at("fixture-heap", cursor) {
        HeapState::Known(state) => state,
        state => panic!("expected known state, got {state:?}"),
    }
}

fn unknown(replay: &HeapReplay, cursor: ReplayCursor, reason: HeapUnknownReason) {
    match replay.heap_state_at("fixture-heap", cursor) {
        HeapState::Unknown(state) => assert_eq!(state.reason, reason, "{}", state.detail),
        state => panic!("expected {reason:?}, got {state:?}"),
    }
}

fn recovery(value: &mut Value, sequence: u64, time_ns: u64) {
    value["records"].as_array_mut().unwrap().push(json!({"kind":"checkpoint", "heap_epoch":"heap-epoch-1", "sequence":sequence, "time_ns":time_ns,"chunks":baseline(),"label":"Independent complete recovery"}));
}

#[test]
fn splits_and_bidirectional_coalescing_reconstruct_exact_extents() {
    let replay = load(&journal());
    assert_eq!(replay.points.len(), 5);
    assert_eq!(
        known(&replay, ReplayCursor::Time(100)).evidence,
        HeapEvidence::ObservedCheckpoint
    );
    assert_eq!(
        known(&replay, ReplayCursor::Time(150)).evidence,
        HeapEvidence::Reconstructed
    );
    let allocated = known(&replay, ReplayCursor::Sequence(2));
    assert_eq!(
        allocated
            .chunks
            .iter()
            .map(|c| (c.start, c.size))
            .collect::<Vec<_>>(),
        vec![(4096, 16), (4112, 48), (4160, 80), (4240, 96), (4336, 16)]
    );
    let released_left = known(&replay, ReplayCursor::Time(450));
    assert_eq!(released_left.sequence, 3);
    assert_eq!(
        released_left.chunks[1].state,
        nxrs_memory_explorer::HeapChunkState::Free
    );
    assert_eq!(
        released_left.chunks[2].state,
        nxrs_memory_explorer::HeapChunkState::Allocated
    );
    assert_eq!(
        known(&replay, ReplayCursor::Sequence(4)).chunks,
        known(&replay, ReplayCursor::Sequence(0)).chunks
    );
    assert_eq!(known(&replay, ReplayCursor::Time(600)).sequence, 4);
    unknown(
        &replay,
        ReplayCursor::Time(601),
        HeapUnknownReason::AfterCoverage,
    );
    unknown(
        &replay,
        ReplayCursor::Time(99),
        HeapUnknownReason::BeforeCapture,
    );
}

#[test]
fn mutation_windows_are_unknown_and_every_commit_is_navigable() {
    let replay = load(&journal());
    for time in [200, 201, 219, 300, 319, 500, 519] {
        unknown(
            &replay,
            ReplayCursor::Time(time),
            HeapUnknownReason::MutationInProgress,
        );
    }
    for (id, sequence, time) in [(1, 1, 220), (2, 2, 320), (3, 3, 420), (4, 4, 520)] {
        let point = known(&replay, ReplayCursor::Point(id));
        assert_eq!((point.sequence, point.time_ns), (sequence, time));
        assert_eq!(known(&replay, ReplayCursor::Time(time)).sequence, sequence);
    }
    assert_eq!(
        replay
            .coverage
            .iter()
            .filter(|range| range.kind == ReplayCoverageKind::Mutation)
            .count(),
        4
    );
}

#[test]
fn no_op_releases_consume_sequences_and_do_not_disappear() {
    let mut value = journal();
    value["records"].as_array_mut().unwrap().push(transaction(
        5,
        550,
        4112,
        224,
        vec![chunk(4112, 224, "free")],
        vec![chunk(4112, 224, "free")],
    ));
    value["closing"]["sequence"] = json!(5);
    let replay = load(&value);
    assert_eq!(replay.points.len(), 6);
    assert_eq!(
        known(&replay, ReplayCursor::Sequence(4)).chunks,
        known(&replay, ReplayCursor::Sequence(5)).chunks
    );
    assert_eq!(known(&replay, ReplayCursor::Time(580)).sequence, 5);
}

#[test]
fn a_dropped_cancelling_pair_is_not_hidden_by_equal_endpoints() {
    let mut value = journal();
    value["records"] = json!([]);
    recovery(&mut value, 4, 550);
    let replay = load(&value);
    assert_eq!(
        known(&replay, ReplayCursor::Point(0)).chunks,
        known(&replay, ReplayCursor::Point(1)).chunks
    );
    unknown(
        &replay,
        ReplayCursor::Time(150),
        HeapUnknownReason::SequenceGap,
    );
    unknown(
        &replay,
        ReplayCursor::Time(540),
        HeapUnknownReason::SequenceGap,
    );
    assert_eq!(
        known(&replay, ReplayCursor::Time(550)).evidence,
        HeapEvidence::ObservedCheckpoint
    );
    assert_eq!(known(&replay, ReplayCursor::Time(560)).sequence, 4);
}

#[test]
fn missing_transaction_invalidates_from_last_proven_boundary_until_recovery() {
    let mut value = journal();
    value["records"].as_array_mut().unwrap().remove(1);
    recovery(&mut value, 4, 550);
    let replay = load(&value);
    assert_eq!(known(&replay, ReplayCursor::Time(220)).sequence, 1);
    unknown(
        &replay,
        ReplayCursor::Time(221),
        HeapUnknownReason::SequenceGap,
    );
    unknown(
        &replay,
        ReplayCursor::Sequence(3),
        HeapUnknownReason::UnknownSequence,
    );
    assert_eq!(known(&replay, ReplayCursor::Time(551)).sequence, 4);
}

#[test]
fn duplicate_and_reordered_records_require_complete_recovery() {
    let mut duplicated = journal();
    let duplicate = duplicated["records"][1].clone();
    duplicated["records"]
        .as_array_mut()
        .unwrap()
        .insert(2, duplicate);
    recovery(&mut duplicated, 4, 550);
    let replay = load(&duplicated);
    assert_eq!(known(&replay, ReplayCursor::Time(250)).sequence, 1);
    unknown(
        &replay,
        ReplayCursor::Time(350),
        HeapUnknownReason::SequenceOrder,
    );
    assert_eq!(known(&replay, ReplayCursor::Time(560)).sequence, 4);
    let mut reordered = journal();
    reordered["records"].as_array_mut().unwrap().swap(0, 1);
    recovery(&mut reordered, 4, 550);
    let replay = load(&reordered);
    unknown(
        &replay,
        ReplayCursor::Time(150),
        HeapUnknownReason::SequenceGap,
    );
    assert_eq!(known(&replay, ReplayCursor::Time(560)).sequence, 4);
}

#[test]
fn contradictory_old_geometry_is_atomic_and_recovery_does_not_rewrite_history() {
    let mut value = journal();
    value["records"][2]["expected"][0]["state"] = json!("free");
    recovery(&mut value, 4, 550);
    let replay = load(&value);
    unknown(
        &replay,
        ReplayCursor::Time(350),
        HeapUnknownReason::Contradiction,
    );
    assert_eq!(
        known(&replay, ReplayCursor::Sequence(2)).chunks[1].state,
        nxrs_memory_explorer::HeapChunkState::Allocated
    );
    unknown(
        &replay,
        ReplayCursor::Sequence(3),
        HeapUnknownReason::UnknownSequence,
    );
    assert_eq!(known(&replay, ReplayCursor::Time(560)).chunks.len(), 3);
}

#[test]
fn invalid_replacement_gap_overlap_overflow_and_boundary_never_partially_apply() {
    for mutation in 0..5 {
        let mut value = journal();
        match mutation {
            0 => value["records"][0]["replacement"][1]["start"] = json!(4161),
            1 => value["records"][0]["replacement"][1]["start"] = json!(4159),
            2 => value["records"][0]["replacement"][1]["size"] = json!(u64::MAX),
            3 => value["records"][0]["replacement"] = json!([]),
            _ => value["records"][0]["start"] = json!(4113),
        }
        let replay = load(&value);
        unknown(
            &replay,
            ReplayCursor::Time(150),
            HeapUnknownReason::InvalidTopology,
        );
        assert_eq!(replay.points.len(), 1);
        assert_eq!(known(&replay, ReplayCursor::Sequence(0)).chunks.len(), 3);
    }
}

#[test]
fn heap_epoch_change_invalidates_only_until_same_epoch_full_recovery() {
    let mut value = journal();
    value["records"][1]["heap_epoch"] = json!("reinitialized-heap");
    recovery(&mut value, 4, 550);
    let replay = load(&value);
    unknown(
        &replay,
        ReplayCursor::Time(250),
        HeapUnknownReason::EpochMismatch,
    );
    assert_eq!(known(&replay, ReplayCursor::Time(560)).sequence, 4);
    let last = value["records"].as_array().unwrap().len() - 1;
    value["records"][last]["heap_epoch"] = json!("reinitialized-heap");
    let replay = load(&value);
    unknown(
        &replay,
        ReplayCursor::Time(560),
        HeapUnknownReason::EpochMismatch,
    );
}

#[test]
fn unlocated_loss_does_not_hold_state_until_detection_time() {
    let mut value = journal();
    value["records"].as_array_mut().unwrap().insert(1,json!({"kind":"loss","heap_epoch":"heap-epoch-1","time_ns":290,"reason":"transport overwrite"}));
    recovery(&mut value, 4, 550);
    let replay = load(&value);
    assert_eq!(known(&replay, ReplayCursor::Time(220)).sequence, 1);
    unknown(&replay, ReplayCursor::Time(221), HeapUnknownReason::Loss);
    unknown(&replay, ReplayCursor::Time(280), HeapUnknownReason::Loss);
    assert_eq!(known(&replay, ReplayCursor::Time(560)).sequence, 4);
}

#[test]
fn unclosed_and_incorrectly_closed_capture_has_no_tail_hold() {
    let mut value = journal();
    value["closing"] = Value::Null;
    let replay = load(&value);
    assert_eq!(known(&replay, ReplayCursor::Time(450)).sequence, 3);
    assert_eq!(known(&replay, ReplayCursor::Time(520)).sequence, 4);
    unknown(
        &replay,
        ReplayCursor::Time(521),
        HeapUnknownReason::UnclosedCapture,
    );
    let mut value = journal();
    value["records"].as_array_mut().unwrap().pop();
    let replay = load(&value);
    unknown(
        &replay,
        ReplayCursor::Time(450),
        HeapUnknownReason::InvalidClosing,
    );
    unknown(
        &replay,
        ReplayCursor::Time(600),
        HeapUnknownReason::InvalidClosing,
    );
    let broken = journal().to_string();
    assert!(parse_heap_journal(&broken[..broken.len() - 3], &memory(), None).is_err());
}

#[test]
fn equal_ticks_require_sequence_or_explicit_point_navigation() {
    let mut value = journal();
    for record in value["records"].as_array_mut().unwrap() {
        record["start_ns"] = json!(200);
        record["commit_ns"] = json!(200);
    }
    let replay = load(&value);
    assert_eq!(replay.points.len(), 5);
    unknown(
        &replay,
        ReplayCursor::Time(200),
        HeapUnknownReason::AmbiguousTimestamp,
    );
    assert_eq!(known(&replay, ReplayCursor::Sequence(1)).chunks.len(), 4);
    assert_eq!(known(&replay, ReplayCursor::Sequence(2)).chunks.len(), 5);
    assert_eq!(known(&replay, ReplayCursor::Time(201)).sequence, 4);
}

#[test]
fn commit_at_next_begin_tick_is_unknown_by_time_but_retains_exact_point() {
    let mut value = journal();
    value["records"][1]["start_ns"] = json!(220);
    let replay = load(&value);
    unknown(
        &replay,
        ReplayCursor::Time(220),
        HeapUnknownReason::MutationInProgress,
    );
    assert_eq!(known(&replay, ReplayCursor::Sequence(1)).sequence, 1);
}

#[test]
fn complete_checkpoint_can_recover_but_cannot_hide_unrecorded_change() {
    let mut value = journal();
    value["records"] = json!([]);
    recovery(&mut value, 0, 550);
    value["records"][0]["chunks"][1]["state"] = json!("allocated");
    value["closing"]["sequence"] = json!(0);
    let replay = load(&value);
    unknown(
        &replay,
        ReplayCursor::Time(200),
        HeapUnknownReason::Contradiction,
    );
    unknown(
        &replay,
        ReplayCursor::Sequence(0),
        HeapUnknownReason::AmbiguousSequence,
    );
    assert_eq!(
        known(&replay, ReplayCursor::Point(0)).chunks[1].state,
        nxrs_memory_explorer::HeapChunkState::Free
    );
    assert_eq!(
        known(&replay, ReplayCursor::Point(1)).chunks[1].state,
        nxrs_memory_explorer::HeapChunkState::Allocated
    );
}

#[test]
fn bare_topology_cannot_sneak_in_unvalidated_generation_or_source() {
    for field in ["generation", "allocation_id", "source", "backtrace"] {
        let mut value = journal();
        value["initial"]["chunks"][1][field] = json!(42);
        assert!(
            parse_heap_journal(&value.to_string(), &memory(), None).is_err(),
            "accepted {field}"
        );
    }
    let replay = load(&journal());
    let spans = known(&replay, ReplayCursor::Sequence(2)).spans();
    assert!(spans
        .iter()
        .all(|span| span.source.is_none() && span.backtrace.is_empty()));
    assert!(spans
        .iter()
        .all(|span| !matches!(span.kind, SpanKind::Stack | SpanKind::StackUsed)));
    assert_eq!(spans[0].kind, SpanKind::HeapArena);
}

#[test]
fn replay_never_changes_exact_snapshot_contract_or_borrows_another_runtime_layer() {
    let model = memory();
    let replay = parse_heap_journal(&journal().to_string(), &model, None).unwrap();
    assert!(model.snapshot_at(251).is_none());
    assert_eq!(model.snapshot_at(250).unwrap().spans.len(), 1);
    let spans = known(&replay, ReplayCursor::Time(250)).spans();
    assert!(spans.iter().all(|span| span.start < 5000));
    assert!(
        matches!(replay.heap_state_at("another-heap",ReplayCursor::Time(250)),HeapState::Unknown(state) if state.reason==HeapUnknownReason::UnknownHeap)
    );
}

#[test]
fn header_and_region_identity_are_admitted_before_reconstruction() {
    for (field, value) in [
        ("schema_version", json!(2)),
        ("recording_id", json!("bad token")),
        ("clock", json!("")),
    ] {
        let mut document = journal();
        document[field] = value;
        assert!(parse_heap_journal(&document.to_string(), &memory(), None).is_err());
    }
    for field in ["nuttx_revision", "config_sha256"] {
        let mut document = journal();
        document["provenance"][field] = json!("not-a-digest");
        assert!(parse_heap_journal(&document.to_string(), &memory(), None).is_err());
    }
    let mut document = journal();
    document["build"]["elf_sha256"] = json!("c".repeat(64));
    assert!(parse_heap_journal(&document.to_string(), &memory(), None).is_err());
    let mut document = journal();
    document["heap"]["region"] = json!("not-mapped");
    assert!(parse_heap_journal(&document.to_string(), &memory(), None).is_err());
    let mut document = journal();
    document["initial"]["sequence"] = json!(1);
    assert!(parse_heap_journal(&document.to_string(), &memory(), None).is_err());
    let mut model = memory();
    model.regions[0].domain = AddressDomain::Image;
    assert!(parse_heap_journal(&journal().to_string(), &model, None).is_err());
}

#[test]
fn optional_trace_requires_exact_recording_build_clock_and_representable_origin() {
    let value = json!({"otherData":{"nxrs":{"schema_version":1,"recording_id":"recording-1","clock":"target-clock","origin_ns":50,"elf_sha256":"a".repeat(64)}},"traceEvents":[{"ph":"i","name":"start","pid":1,"ts":0},{"ph":"i","name":"end","pid":1,"ts":1}]});
    let trace = parse_timing(&value.to_string()).unwrap();
    assert!(parse_heap_journal(&journal().to_string(), &memory(), Some(&trace)).is_ok());
    for field in ["recording_id", "clock", "elf_sha256", "origin_ns"] {
        let mut altered = value.clone();
        altered["otherData"]["nxrs"][field] = match field {
            "origin_ns" => json!(101),
            "elf_sha256" => json!("d".repeat(64)),
            _ => json!("different"),
        };
        let trace = parse_timing(&altered.to_string()).unwrap();
        assert!(
            parse_heap_journal(&journal().to_string(), &memory(), Some(&trace)).is_err(),
            "accepted wrong {field}"
        );
    }
}

#[test]
fn integer_geometry_and_source_time_preserve_high_bits() {
    let mut value = journal();
    let address_offset = 1u64 << 60;
    let time_offset = u64::MAX - 700;
    value["heap"]["start"] = json!(4096 + address_offset);
    value["initial"]["time_ns"] = json!(100 + time_offset);
    for part in value["initial"]["chunks"].as_array_mut().unwrap() {
        part["start"] = json!(part["start"].as_u64().unwrap() + address_offset);
    }
    for record in value["records"].as_array_mut().unwrap() {
        for field in ["start_ns", "commit_ns"] {
            record[field] = json!(record[field].as_u64().unwrap() + time_offset);
        }
        record["start"] = json!(record["start"].as_u64().unwrap() + address_offset);
        for field in ["expected", "replacement"] {
            for part in record[field].as_array_mut().unwrap() {
                part["start"] = json!(part["start"].as_u64().unwrap() + address_offset);
            }
        }
    }
    value["closing"]["time_ns"] = json!(600 + time_offset);
    let mut model = memory();
    model.regions[0].start += address_offset;
    model.snapshots.clear();
    let replay = parse_heap_journal(&value.to_string(), &model, None).unwrap();
    let state = known(&replay, ReplayCursor::Time(221 + time_offset));
    assert_eq!(state.chunks[1].start, 4112 + address_offset);
    assert_eq!(state.chunks[1].size, 48);
    assert_eq!(state.time_ns, 221 + time_offset);
}

#[test]
fn sequence_overflow_clock_reversal_and_invalid_footer_fail_closed() {
    let mut value = journal();
    value["records"][1]["start_ns"] = json!(219);
    let replay = load(&value);
    unknown(
        &replay,
        ReplayCursor::Time(250),
        HeapUnknownReason::ClockOrder,
    );
    let mut value = journal();
    recovery(&mut value, u64::MAX, 550);
    value["records"].as_array_mut().unwrap().push(transaction(
        0,
        560,
        4112,
        224,
        vec![chunk(4112, 224, "free")],
        vec![chunk(4112, 224, "free")],
    ));
    value["closing"]["sequence"] = json!(u64::MAX);
    let replay = load(&value);
    unknown(
        &replay,
        ReplayCursor::Time(590),
        HeapUnknownReason::SequenceOrder,
    );
    let mut value = journal();
    value["closing"]["heap_epoch"] = json!("other");
    let replay = load(&value);
    unknown(
        &replay,
        ReplayCursor::Time(550),
        HeapUnknownReason::InvalidClosing,
    );
}

#[test]
fn declared_resource_bounds_limit_replay_expansion() {
    let mut too_many_records = journal();
    too_many_records["records"] = json!(vec![
        json!({
            "kind":"loss", "heap_epoch":"heap-epoch-1", "time_ns":100, "reason":"lost"
        });
        4097
    ]);
    assert!(
        parse_heap_journal(&too_many_records.to_string(), &memory(), None)
            .unwrap_err()
            .to_string()
            .contains("4096 records")
    );

    // A compact stream of one-chunk no-ops could otherwise force unbounded
    // copies of a large initial topology. Bound retained states independently
    // of input size, record count and each individual topology.
    let mut expanded = journal();
    expanded["heap"]["size"] = json!(1024);
    expanded["initial"]["chunks"] = json!((4096..5120)
        .map(|address| chunk(address, 1, "free"))
        .collect::<Vec<_>>());
    expanded["records"] = json!((1..=1000)
        .map(|sequence| transaction(
            sequence,
            200 + sequence * 40,
            4096,
            1,
            vec![chunk(4096, 1, "free")],
            vec![chunk(4096, 1, "free")]
        ))
        .collect::<Vec<_>>());
    expanded["closing"] = Value::Null;
    assert!(parse_heap_journal(&expanded.to_string(), &memory(), None)
        .unwrap_err()
        .to_string()
        .contains("one million retained"));
}

#[test]
fn schema_rejects_fractional_coordinates_duplicate_fields_and_unknown_receipts() {
    let mut fractional = journal();
    fractional["initial"]["time_ns"] = json!(100.5);
    assert!(parse_heap_journal(&fractional.to_string(), &memory(), None).is_err());
    let text = journal().to_string();
    let duplicate = text.replacen(
        "\"schema_version\":1",
        "\"schema_version\":1,\"schema_version\":1",
        1,
    );
    assert!(parse_heap_journal(&duplicate, &memory(), None).is_err());
    let mut extra = journal();
    extra["build"]["qualified"] = json!(true);
    assert!(parse_heap_journal(&extra.to_string(), &memory(), None).is_err());
    let mut extra = journal();
    extra["closing"]["lost_records"] = json!(0);
    assert!(parse_heap_journal(&extra.to_string(), &memory(), None).is_err());
}

#[test]
fn foreign_epoch_counters_and_clock_cannot_poison_recovery_order() {
    for kind in ["transaction", "checkpoint", "loss"] {
        let mut value = journal();
        let foreign = match kind {
            "transaction" => {
                let mut record = value["records"][1].clone();
                record["heap_epoch"] = json!("foreign-epoch");
                record["sequence"] = json!(u64::MAX);
                record["start_ns"] = json!(u64::MAX - 1);
                record["commit_ns"] = json!(u64::MAX);
                record
            }
            "checkpoint" => {
                json!({"kind":"checkpoint", "heap_epoch":"foreign-epoch", "sequence":u64::MAX, "time_ns":u64::MAX,"chunks":baseline()})
            }
            _ => {
                json!({"kind":"loss", "heap_epoch":"foreign-epoch", "time_ns":u64::MAX, "reason":"other heap reset"})
            }
        };
        value["records"].as_array_mut().unwrap().insert(1, foreign);
        recovery(&mut value, 4, 550);
        let replay = load(&value);
        unknown(
            &replay,
            ReplayCursor::Time(250),
            HeapUnknownReason::EpochMismatch,
        );
        assert_eq!(known(&replay, ReplayCursor::Time(560)).sequence, 4);
        assert_eq!(replay.bounds.end_ns, 600);
    }
}

fn linked_span(region: &str, start: u64, kind: SpanKind) -> nxrs_memory_explorer::Span {
    nxrs_memory_explorer::Span {
        id: "linked-section".into(),
        name: "Linked section".into(),
        region: region.into(),
        start,
        size: 16,
        kind,
        source: None,
        parent: None,
        detail: None,
        backtrace: vec![],
    }
}

#[test]
fn replay_free_geometry_cannot_conflict_with_linked_bytes_or_a_physical_alias() {
    for kind in [SpanKind::Code, SpanKind::ReadOnly, SpanKind::Data] {
        let mut model = memory();
        model.static_spans.push(linked_span("ram", 4112, kind));
        assert!(parse_heap_journal(&journal().to_string(), &model, None).is_err());
    }
    let mut model = memory();
    model.regions[0].physical = Some(nxrs_memory_explorer::PhysicalBacking {
        id: "backing".into(),
        offset: 0,
    });
    model
        .physical_resources
        .push(nxrs_memory_explorer::PhysicalResource {
            id: "backing".into(),
            name: "SRAM".into(),
            size: 4096,
            banks: vec![],
        });
    let mut alias = model.regions[0].clone();
    alias.id = "alias".into();
    alias.start = 32768;
    model.regions.push(alias);
    model
        .static_spans
        .push(linked_span("alias", 32784, SpanKind::Code));
    assert!(parse_heap_journal(&journal().to_string(), &model, None)
        .unwrap_err()
        .to_string()
        .contains("physical resource"));

    // A BSS/Reserved arena is the legitimate storage reservation being refined
    // by the journal. It must not make all observed free chunks contradictory.
    model.static_spans.clear();
    model
        .static_spans
        .push(linked_span("ram", 4112, SpanKind::Bss));
    assert!(parse_heap_journal(&journal().to_string(), &model, None).is_ok());
    model.regions[0].kind = nxrs_memory_explorer::RegionKind::Flash;
    assert!(parse_heap_journal(&journal().to_string(), &model, None).is_err());
}

#[test]
fn a_later_conflicting_free_replacement_cannot_become_known() {
    let mut model = memory();
    model
        .static_spans
        .push(linked_span("ram", 4112, SpanKind::Code));
    let mut value = journal();
    value["initial"]["chunks"][1]["state"] = json!("allocated");
    value["records"] = json!([transaction(
        1,
        200,
        4112,
        224,
        vec![chunk(4112, 224, "allocated")],
        vec![chunk(4112, 224, "free")]
    )]);
    value["closing"]["sequence"] = json!(1);
    recovery(&mut value, 1, 550);
    let replay = parse_heap_journal(&value.to_string(), &model, None).unwrap();
    assert_eq!(replay.points.len(), 1);
    unknown(
        &replay,
        ReplayCursor::Time(150),
        HeapUnknownReason::InvalidTopology,
    );
    unknown(
        &replay,
        ReplayCursor::Time(560),
        HeapUnknownReason::InvalidTopology,
    );
}
