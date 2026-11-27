use nxrs_memory_explorer::{
    correlate_checkpoints, parse_snapshot, parse_timing, AddressRange, MemoryModel, TimeConfidence,
    TimingEventKind,
};
use serde_json::{json, Value};

fn snapshot_document() -> Value {
    json!({
        "schema_version": 1,
        "build": {"elf_sha256": "a".repeat(64)},
        "regions": [{"id":"ram", "name":"RAM", "start":4096, "size":256, "kind":"ram"}],
        "snapshots": [
            {"time_ns":1000, "label":"ready", "complete_regions":["ram"],
             "correlation":{"recording_id":"recording-1", "clock":"target-clock", "checkpoint_id":1},
             "spans":[
                {"id":"heap", "name":"Heap", "region":"ram", "start":4096, "size":256, "kind":"heap_arena"},
                {"id":"allocation", "name":"allocation", "region":"ram", "start":4096, "size":32, "kind":"allocation", "parent":"heap"},
                {"id":"free", "name":"free", "region":"ram", "start":4128, "size":224, "kind":"free", "parent":"heap"}
             ]},
            {"time_ns":2000, "label":"after allocation", "complete_regions":["ram"],
             "correlation":{"recording_id":"recording-1", "clock":"target-clock", "checkpoint_id":3},
             "spans":[
                {"id":"heap", "name":"Heap", "region":"ram", "start":4096, "size":256, "kind":"heap_arena"},
                {"id":"allocation", "name":"allocation", "region":"ram", "start":4096, "size":64, "kind":"allocation", "parent":"heap"},
                {"id":"free", "name":"free", "region":"ram", "start":4160, "size":192, "kind":"free", "parent":"heap"}
             ]}
        ]
    })
}

fn trace_document() -> Value {
    // The trace is intentionally out of record order. Event IDs refer to raw
    // input positions; their positions in the sorted import are different.
    serde_json::from_str(&format!(
        r#"{{
          "otherData":{{"nxrs":{{"schema_version":1,"recording_id":"recording-1",
            "clock":"target-clock","origin_ns":900,"elf_sha256":"{}",
            "lost_records":0,"loss_detected":false,"complete":true}}}},
          "traceEvents":[
            {{"ph":"i","name":"memory/checkpoint/3","pid":1,"ts":1.1}},
            {{"ph":"i","name":"memory/checkpoint/1","pid":1,"ts":0.1}},
            {{"ph":"i","name":"memory/checkpoint/2","pid":1,"ts":0.6}}
          ]
        }}"#,
        "a".repeat(64)
    ))
    .unwrap()
}

fn model(document: &Value) -> MemoryModel {
    MemoryModel::from_snapshot(parse_snapshot(&document.to_string()).unwrap()).unwrap()
}

#[test]
fn exact_marker_links_keep_record_identity_and_unobserved_gaps() {
    let model = model(&snapshot_document());
    let trace = parse_timing(&trace_document().to_string()).unwrap();
    let correlation = correlate_checkpoints(&model, &trace).unwrap().unwrap();
    assert_eq!(correlation.recording_id, "recording-1");
    assert_eq!(correlation.clock, "target-clock");
    assert_eq!(correlation.origin_ns, 900);
    assert_eq!(correlation.links.len(), 2);
    let ready = &correlation.links[0];
    assert_eq!(ready.snapshot_index, 0);
    assert_eq!(ready.snapshot_time_ns, 1000);
    assert_eq!(ready.checkpoint_id, 1);
    assert_eq!(ready.event_id, 1);
    assert_eq!(ready.trace_time_ns, 100);
    assert_eq!(correlation.link_for_event(0).unwrap().checkpoint_id, 3);
    assert_eq!(
        correlation.link_for_snapshot(2000).unwrap().trace_time_ns,
        1100
    );
    assert!(correlation.link_for_event(2).is_none());
    assert!(correlation.link_at_trace_time(101).is_none());
    assert_eq!(correlation.link_at_trace_time(100), Some(ready));

    let between = correlation.source_time_ns(600).unwrap();
    assert_eq!(between, 1500);
    let gap = model.view("ram", AddressRange::new(4096, 256).unwrap(), between);
    assert_eq!(gap.runtime, TimeConfidence::Unknown);
    assert!(gap.spans.is_empty());
    assert_eq!(model.free_bytes("ram", 1000), 224);
    assert_eq!(model.free_bytes("ram", 2000), 192);
    assert!(correlation.source_time_ns(u64::MAX).is_none());
}

#[test]
fn legacy_snapshots_do_not_align_from_matching_build_clock_or_numbers() {
    let mut snapshots = snapshot_document();
    for point in snapshots["snapshots"].as_array_mut().unwrap() {
        point.as_object_mut().unwrap().remove("correlation");
    }
    let model = model(&snapshots);
    let trace = parse_timing(&trace_document().to_string()).unwrap();
    assert!(correlate_checkpoints(&model, &trace).unwrap().is_none());
    let encoded = serde_json::to_value(parse_snapshot(&snapshots.to_string()).unwrap()).unwrap();
    assert!(encoded["snapshots"][0].get("correlation").is_none());
}

#[test]
fn correlated_snapshot_files_reject_mixed_recordings_clocks_and_sequences() {
    let mut cases = Vec::new();
    let mut missing = snapshot_document();
    missing["snapshots"][0]
        .as_object_mut()
        .unwrap()
        .remove("correlation");
    cases.push(missing);
    for (field, replacement) in [
        ("recording_id", json!("recording-2")),
        ("clock", json!("another-clock")),
        ("checkpoint_id", json!(1)),
        ("checkpoint_id", json!(0)),
    ] {
        let mut document = snapshot_document();
        document["snapshots"][1]["correlation"][field] = replacement;
        cases.push(document);
    }
    let mut reversed = snapshot_document();
    reversed["snapshots"][0]["correlation"]["checkpoint_id"] = json!(4);
    cases.push(reversed);
    for document in cases {
        assert!(
            parse_snapshot(&document.to_string()).is_err(),
            "accepted {document}"
        );
    }
}

#[test]
fn recording_tokens_are_bounded_portable_and_nonempty() {
    for token in [
        "".to_owned(),
        "with space".into(),
        "with\nnewline".into(),
        "é".into(),
        "\u{7f}".into(),
        "x".repeat(129),
    ] {
        let mut document = snapshot_document();
        for point in document["snapshots"].as_array_mut().unwrap() {
            point["correlation"]["recording_id"] = json!(token);
        }
        assert!(parse_snapshot(&document.to_string()).is_err());
        let mut trace = trace_document();
        trace["otherData"]["nxrs"]["recording_id"] = json!(token);
        assert!(parse_timing(&trace.to_string()).is_err());
    }
    let mut trace = trace_document();
    trace["otherData"]["nxrs"]["recording_id"] = json!("x".repeat(128));
    assert!(parse_timing(&trace.to_string()).is_ok());
    let mut document = snapshot_document();
    for point in document["snapshots"].as_array_mut().unwrap() {
        point["correlation"]["clock"] = json!("  ");
    }
    assert!(parse_snapshot(&document.to_string()).is_err());
}

#[test]
fn every_identity_component_is_required_for_claimed_links() {
    let model = model(&snapshot_document());
    for field in ["recording_id", "clock", "origin_ns", "elf_sha256"] {
        let mut document = trace_document();
        document["otherData"]["nxrs"]
            .as_object_mut()
            .unwrap()
            .remove(field);
        let trace = parse_timing(&document.to_string()).unwrap();
        assert!(
            correlate_checkpoints(&model, &trace).is_err(),
            "accepted absent {field}"
        );
    }
    for (field, replacement) in [
        ("recording_id", json!("other-recording")),
        ("clock", json!("other-clock")),
        ("origin_ns", json!(901)),
        ("elf_sha256", json!("b".repeat(64))),
    ] {
        let mut document = trace_document();
        document["otherData"]["nxrs"][field] = replacement;
        let trace = parse_timing(&document.to_string()).unwrap();
        assert!(
            correlate_checkpoints(&model, &trace).is_err(),
            "accepted changed {field}"
        );
    }
    let mut document = trace_document();
    document["otherData"]["nxrs"]["elf_sha256"] = json!("A".repeat(64));
    assert!(correlate_checkpoints(&model, &parse_timing(&document.to_string()).unwrap()).is_ok());
}

#[test]
fn missing_duplicate_and_wrong_kind_markers_are_rejected() {
    let model = model(&snapshot_document());
    let mut missing = trace_document();
    missing["traceEvents"].as_array_mut().unwrap().remove(1);
    let trace = parse_timing(&missing.to_string()).unwrap();
    assert!(correlate_checkpoints(&model, &trace)
        .unwrap_err()
        .to_string()
        .contains("missing checkpoint marker"));

    let mut duplicate = trace_document();
    duplicate["traceEvents"]
        .as_array_mut()
        .unwrap()
        .push(json!({"ph":"i", "name":"memory/checkpoint/1", "pid":2, "ts":0.1}));
    let trace = parse_timing(&duplicate.to_string()).unwrap();
    assert!(correlate_checkpoints(&model, &trace)
        .unwrap_err()
        .to_string()
        .contains("duplicate checkpoint marker"));

    for (phase, args) in [("X", json!({})), ("C", json!({"value":1}))] {
        let mut document = trace_document();
        document["traceEvents"][1]["ph"] = json!(phase);
        document["traceEvents"][1]["dur"] = json!(0);
        document["traceEvents"][1]["args"] = args;
        let trace = parse_timing(&document.to_string()).unwrap();
        assert!(correlate_checkpoints(&model, &trace)
            .unwrap_err()
            .to_string()
            .contains("must be an instant"));
    }
}

#[test]
fn reserved_checkpoint_names_cannot_alias_or_overflow_an_id() {
    let model = model(&snapshot_document());
    for suffix in [
        "0",
        "01",
        "+1",
        "-1",
        "1.0",
        "1/again",
        "18446744073709551616",
        "",
    ] {
        let mut document = trace_document();
        document["traceEvents"][1]["name"] = json!(format!("memory/checkpoint/{suffix}"));
        let trace = parse_timing(&document.to_string()).unwrap();
        assert!(
            correlate_checkpoints(&model, &trace).is_err(),
            "accepted {suffix}"
        );
    }
}

#[test]
fn one_nanosecond_timestamp_mismatch_is_not_rounded_or_guessed() {
    let mut document = snapshot_document();
    document["snapshots"][0]["time_ns"] = json!(1001);
    let trace = parse_timing(&trace_document().to_string()).unwrap();
    assert!(correlate_checkpoints(&model(&document), &trace)
        .unwrap_err()
        .to_string()
        .contains("timestamp differs"));
}

#[test]
fn high_source_origins_preserve_adjacent_integer_nanoseconds() {
    let mut snapshots = snapshot_document();
    snapshots["snapshots"][0]["time_ns"] = json!((1u64 << 53) + 1);
    snapshots["snapshots"][1]["time_ns"] = json!((1u64 << 53) + 2);
    let model = model(&snapshots);
    let text = format!(
        r#"{{"otherData":{{"nxrs":{{"recording_id":"recording-1","clock":"target-clock","origin_ns":{},"elf_sha256":"{}"}}}},"traceEvents":[{{"ph":"i","pid":1,"name":"memory/checkpoint/1","ts":0.001}},{{"ph":"i","pid":1,"name":"memory/checkpoint/3","ts":0.002}}]}}"#,
        1u64 << 53,
        "a".repeat(64)
    );
    let trace = parse_timing(&text).unwrap();
    let correlation = correlate_checkpoints(&model, &trace).unwrap().unwrap();
    assert_eq!(correlation.links[0].trace_time_ns, 1);
    assert_eq!(correlation.links[1].trace_time_ns, 2);
    assert_eq!(correlation.source_time_ns(1), Some((1u64 << 53) + 1));

    let mut overflowing = trace;
    overflowing.metadata.origin_ns = Some(u64::MAX);
    assert!(correlate_checkpoints(&model, &overflowing)
        .unwrap_err()
        .to_string()
        .contains("overflows"));
}

#[test]
fn reported_loss_does_not_erase_independently_observed_checkpoint_but_gaps_stay_unknown() {
    let model = model(&snapshot_document());
    let mut document = trace_document();
    document["otherData"]["nxrs"]["lost_records"] = json!(2);
    document["otherData"]["nxrs"]["loss_detected"] = json!(true);
    document["otherData"]["nxrs"]["complete"] = json!(false);
    let trace = parse_timing(&document.to_string()).unwrap();
    let correlation = correlate_checkpoints(&model, &trace).unwrap().unwrap();
    assert_eq!(correlation.links.len(), 2);
    assert!(trace
        .warnings
        .iter()
        .any(|warning| warning.contains("loss was reported")));
    assert!(correlation.link_at_trace_time(500).is_none());
    assert_eq!(
        model
            .view(
                "ram",
                AddressRange::new(4096, 256).unwrap(),
                correlation.source_time_ns(500).unwrap()
            )
            .runtime,
        TimeConfidence::Unknown
    );
}

#[test]
fn direct_model_mutations_do_not_bypass_correlation_admission() {
    let mut model = model(&snapshot_document());
    let trace = parse_timing(&trace_document().to_string()).unwrap();
    model.snapshots[1].correlation = None;
    assert!(correlate_checkpoints(&model, &trace).is_err());
    let mut trace = trace;
    trace.events[0].kind = TimingEventKind::Interval;
    let model = self::model(&snapshot_document());
    assert!(correlate_checkpoints(&model, &trace).is_err());
}
