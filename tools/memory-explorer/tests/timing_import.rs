use nxrs_memory_explorer::{parse_timing, timing_demo, TimingEventKind, TimingRange};

#[test]
fn decimal_microseconds_preserve_nanoseconds_above_float_precision() {
    let trace = parse_timing(
        r#"{"displayTimeUnit":"ns","traceEvents":[
        {"ph":"X","pid":1,"tid":9,"name":"one nanosecond","ts":9007199254740.993,"dur":0.001}
    ]}"#,
    )
    .unwrap();
    assert_eq!(trace.events[0].start_ns, (1u64 << 53) + 1);
    assert_eq!(trace.events[0].end_ns, (1u64 << 53) + 2);
    assert_eq!(trace.events[0].duration_ns(), 1);
    let ordinary = parse_timing(
        r#"{"displayTimeUnit":"ns","traceEvents":[
        {"ph":"X","pid":1,"name":"microseconds remain microseconds","ts":1,"dur":2}
    ]}"#,
    )
    .unwrap();
    assert_eq!(ordinary.events[0].start_ns, 1000);
    assert_eq!(ordinary.events[0].duration_ns(), 2000);
}

#[test]
fn timestamp_limit_and_exponents_are_exact() {
    let trace = parse_timing(
        r#"[
      {"ph":"X","pid":1,"name":"edge","ts":18446744073709551.614,"dur":1e-3},
      {"ph":"i","pid":2,"name":"point at maximum","ts":18446744073709551.615}
    ]"#,
    )
    .unwrap();
    assert_eq!(trace.bounds.end_ns, u64::MAX);
    assert_eq!(trace.events[0].duration_ns(), 1);
    let exponent =
        parse_timing(r#"[{"ph":"X","pid":1,"name":"decimal","ts":1.234e1,"dur":1e-3}]"#).unwrap();
    assert_eq!(exponent.events[0].start_ns, 12_340);
    assert_eq!(exponent.events[0].duration_ns(), 1);
}

#[test]
fn source_clock_origin_must_fit_the_observed_extent() {
    let edge = format!(
        r#"{{"otherData":{{"nxrs":{{"origin_ns":{}}}}},"traceEvents":[{{"ph":"X","pid":1,"ts":0,"dur":0.001,"name":"edge"}}]}}"#,
        u64::MAX - 1
    );
    let valid = parse_timing(&edge).unwrap();
    assert_eq!(
        valid.metadata.origin_ns.unwrap() + valid.bounds.end_ns,
        u64::MAX
    );
    let overflow = edge.replace(&(u64::MAX - 1).to_string(), &u64::MAX.to_string());
    assert!(parse_timing(&overflow)
        .unwrap_err()
        .to_string()
        .contains("source-clock origin"));
}

#[test]
fn excessive_nesting_has_an_explicit_supported_bound() {
    let interval = serde_json::json!({"ph":"X", "pid":1, "ts":0, "dur":1, "name":"nested"});
    let valid = serde_json::to_string(&vec![interval.clone(); 1024]).unwrap();
    assert_eq!(
        parse_timing(&valid).unwrap().events.last().unwrap().depth,
        1023
    );
    let excess = serde_json::to_string(&vec![interval; 1025]).unwrap();
    assert!(parse_timing(&excess)
        .unwrap_err()
        .to_string()
        .contains("1024-level"));
    let begin = serde_json::json!({"ph":"B", "pid":1, "ts":0, "name":"nested"});
    let excess_begins = serde_json::to_string(&vec![begin; 1025]).unwrap();
    assert!(parse_timing(&excess_begins)
        .unwrap_err()
        .to_string()
        .contains("1024-level"));
}

#[test]
fn begin_end_nesting_and_late_track_names_are_preserved() {
    let trace = parse_timing(
        r#"[
        {"ph":"B","pid":3,"tid":7,"name":"outer","cat":"app","ts":1,"args":{"start":"observed"}},
        {"ph":"B","pid":3,"tid":7,"name":"inner","cat":"","ts":2},
        {"ph":"E","pid":3,"tid":7,"name":"inner","cat":"","ts":3},
        {"ph":"E","pid":3,"tid":7,"ts":5,"args":{"result":"done"}},
        {"ph":"M","pid":3,"name":"process_name","args":{"name":"firmware"}},
        {"ph":"M","pid":3,"tid":7,"name":"thread_name","args":{"name":"worker"}}
    ]"#,
    )
    .unwrap();
    assert_eq!(trace.events[0].name, "outer");
    assert_eq!(trace.events[0].duration_ns(), 4000);
    assert_eq!(trace.events[0].depth, 0);
    assert_eq!(trace.events[1].depth, 1);
    assert_eq!(trace.events[0].args["begin_args"]["start"], "observed");
    assert_eq!(trace.events[0].args["end_args"]["result"], "done");
    assert_eq!(trace.tracks[0].name, "worker");
    assert_eq!(trace.tracks[0].process_name, "firmware");
    assert_eq!(trace.query(trace.bounds, "worker").len(), 2);
}

#[test]
fn invalid_syntax_and_unmeasured_ends_are_rejected() {
    for input in [
        r#"[{"ph":"B","pid":1,"ts":0,"name":"unfinished"}]"#,
        r#"[{"ph":"E","pid":1,"ts":0}]"#,
        r#"[{"ph":"B","pid":1,"ts":2,"name":"x"},{"ph":"E","pid":1,"ts":1}]"#,
        r#"[{"ph":"B","pid":1,"ts":0,"name":"x"},{"ph":"E","pid":1,"ts":1,"name":"y"}]"#,
        r#"[{"ph":"X","pid":1,"ts":0,"dur":-1,"name":"negative"}]"#,
        r#"[{"ph":"X","pid":1,"ts":-1,"dur":1,"name":"negative"}]"#,
        r#"[{"ph":"X","pid":1,"ts":0,"dur":0.0001,"name":"sub-nanosecond"}]"#,
        r#"[{"ph":"X","pid":1,"ts":18446744073709551.615,"dur":0.001,"name":"overflow"}]"#,
        r#"[{"ph":"i","pid":1,"ts":18446744073709551.616,"name":"overflow"}]"#,
        r#"[{"ph":"X","pid":1,"ts":"1","dur":1,"name":"numeric string"}]"#,
        r#"[{"ph":"s","pid":1,"ts":0,"name":"unsupported async"}]"#,
        r#"[{"ph":"C","pid":1,"ts":0,"name":"bad counter","args":{"value":"1"}}]"#,
        r#"[{"ph":"C","pid":1,"ts":0,"name":"bad counter","args":{"value":1e400}}]"#,
    ] {
        assert!(
            parse_timing(input).is_err(),
            "unexpectedly accepted {input}"
        );
    }
}

#[test]
fn crossing_synchronous_intervals_are_not_given_a_fabricated_stack() {
    let invalid = r#"[
        {"ph":"X","pid":1,"ts":0,"dur":10,"name":"a"},
        {"ph":"X","pid":1,"ts":5,"dur":10,"name":"b"}
    ]"#;
    assert!(parse_timing(invalid)
        .unwrap_err()
        .to_string()
        .contains("crossing"));
    let separate_tracks = invalid.replace(r#""ts":5"#, r#""tid":2,"ts":5"#);
    assert_eq!(parse_timing(&separate_tracks).unwrap().events.len(), 2);
}

#[test]
fn selected_coverage_unions_nested_intervals_per_track() {
    let trace = parse_timing(
        r#"[
        {"ph":"X","pid":1,"ts":10,"dur":40,"name":"child"},
        {"ph":"X","pid":1,"ts":0,"dur":100,"name":"parent"},
        {"ph":"X","pid":1,"ts":20,"dur":20,"name":"grandchild"},
        {"ph":"X","pid":2,"ts":0,"dur":100,"name":"independent track"},
        {"ph":"i","pid":1,"ts":20,"name":"ready"}
    ]"#,
    )
    .unwrap();
    let range = TimingRange {
        start_ns: 20_000,
        end_ns: 60_000,
    };
    let stats = trace.stats(range, "");
    assert_eq!(stats.interval_count, 4);
    assert_eq!(stats.instant_count, 1);
    assert_eq!(stats.clipped_interval_sum_ns, 130_000);
    assert_eq!(stats.tracks.len(), 2);
    assert!(stats.tracks.iter().all(|track| track.covered_ns == 40_000));
    assert!(stats
        .tracks
        .iter()
        .all(|track| track.longest_overlap_ns == 40_000));
    let parent = trace.stats(range, "parent");
    assert_eq!(parent.interval_count, 1);
    assert_eq!(parent.tracks[0].covered_ns, 40_000);
    let reversed = trace.stats(
        TimingRange {
            start_ns: 60_000,
            end_ns: 20_000,
        },
        "",
    );
    assert_eq!(reversed.interval_count, 0);
    assert!(reversed.tracks.is_empty());
}

#[test]
fn counters_keep_exact_values_and_do_not_create_durations() {
    let trace = parse_timing(r#"[
        {"ph":"C","pid":1,"tid":3,"ts":1.001,"name":"bytes","args":{"allocated":9007199254740993,"free":-2.25}}
    ]"#).unwrap();
    assert!(trace.events.is_empty());
    assert_eq!(trace.counters.len(), 2);
    assert_eq!(trace.counters[0].time_ns, 1001);
    assert_eq!(trace.counters[0].exact_value, "9007199254740993");
    assert_eq!(trace.bounds.start_ns, trace.bounds.end_ns);
    assert_eq!(trace.counter_query(trace.bounds, "free").len(), 1);
    assert_eq!(trace.stats(trace.bounds, "").clipped_interval_sum_ns, 0);
}

#[test]
fn explicit_provenance_is_preserved_and_unknown_is_not_inferred() {
    let unknown =
        parse_timing(r#"[{"ph":"i","pid":1,"ts":1,"name":"qemu-shaped filename is no proof"}]"#)
            .unwrap();
    assert!(unknown.metadata.source.is_none());
    assert!(unknown.metadata.clock.is_none());
    assert!(unknown.metadata.lost_records.is_none());
    assert!(unknown
        .warnings
        .iter()
        .any(|warning| warning.contains("not synchronized")));
    let known = parse_timing(&format!(r#"{{
      "otherData":{{"nxrs":{{"schema_version":1,"clock":"perf_gettime-converted","source":"declared qemu capture","origin_ns":9007199254740993,"elf_sha256":"{}","loss_detected":true,"complete":false,"incomplete_intervals":2}}}},
      "traceEvents":[{{"ph":"i","pid":1,"ts":0,"name":"incomplete IRQ"}}]
    }}"#, "a".repeat(64))).unwrap();
    assert_eq!(known.metadata.origin_ns, Some((1u64 << 53) + 1));
    assert_eq!(known.metadata.loss_detected, Some(true));
    assert_eq!(known.metadata.lost_records, None);
    assert_eq!(known.metadata.incomplete_intervals, 2);
    assert!(known
        .warnings
        .iter()
        .any(|warning| warning.contains("Record loss")));
}

#[test]
fn contradictory_loss_metadata_is_rejected() {
    for nxrs in [
        r#"{"lost_records":1,"loss_detected":false}"#,
        r#"{"lost_records":0,"loss_detected":true}"#,
        r#"{"elf_sha256":"not-a-hash"}"#,
    ] {
        let trace = format!(
            r#"{{"otherData":{{"nxrs":{nxrs}}},"traceEvents":[{{"ph":"i","pid":1,"ts":0,"name":"point"}}]}}"#
        );
        assert!(parse_timing(&trace).is_err());
    }
}

#[test]
fn zero_width_intervals_and_instants_are_points_not_inferred_work() {
    let trace = parse_timing(
        r#"[
        {"ph":"X","pid":1,"ts":1,"dur":0,"name":"zero recorded duration"},
        {"ph":"I","pid":1,"ts":1,"name":"instant"}
    ]"#,
    )
    .unwrap();
    assert_eq!(trace.events[1].kind, TimingEventKind::Instant);
    let stats = trace.stats(trace.bounds, "");
    assert_eq!(stats.interval_count, 1);
    assert_eq!(stats.instant_count, 1);
    assert_eq!(stats.clipped_interval_sum_ns, 0);
    assert_eq!(stats.tracks[0].covered_ns, 0);
}

#[test]
fn demonstration_is_explicitly_synthetic_and_exercises_all_layers() {
    let demo = timing_demo();
    assert!(demo
        .metadata
        .source
        .as_deref()
        .unwrap()
        .contains("SYNTHETIC"));
    assert!(demo.metadata.elf_sha256.is_none());
    assert!(demo
        .events
        .iter()
        .any(|event| event.category == "irq" && event.depth == 1));
    assert!(demo.events.iter().any(|event| event.category == "app,io"));
    assert!(demo
        .events
        .iter()
        .any(|event| event.kind == TimingEventKind::Instant));
    assert!(!demo.counters.is_empty());
}

#[test]
fn source_identity_uses_recorded_code_and_never_guesses_from_irq_numbers() {
    let trace = parse_timing(r#"[
      {"ph":"X","pid":1,"ts":0,"dur":1,"name":"IRQ 42","cat":"irq","args":{"irq":42}},
      {"ph":"X","pid":1,"ts":1,"dur":2,"name":"IRQ 42","cat":"irq","args":{"irq":42,"handler_function":"gpio_interrupt","handler_file":"chip/gpio.c","caller_function":"unrelated"}},
      {"ph":"B","pid":2,"ts":0,"name":"heap request","cat":"heap","args":{"caller_function":"capture::open","requested_bytes":9007199254740993}},
      {"ph":"E","pid":2,"ts":1,"args":{"caller_function":"different end source"}}
    ]"#).unwrap();
    let irq: Vec<_> = trace.events.iter().filter(|event| event.is_irq()).collect();
    assert_eq!(irq[0].source_name(), None);
    assert_eq!(irq[1].source_name(), Some("gpio_interrupt"));
    let request = trace.events.iter().find(|event| event.is_heap()).unwrap();
    assert_eq!(request.source_name(), Some("capture::open"));
    assert_eq!(request.requested_bytes(), Some(9_007_199_254_740_993));
    assert_eq!(trace.query(trace.bounds, "chip/gpio.c").len(), 1);
}

#[test]
fn source_summaries_clip_intervals_and_keep_unknown_heap_outcomes() {
    let trace = parse_timing(r#"[
      {"ph":"X","pid":1,"ts":0,"dur":100,"name":"work","cat":"app","args":{"caller_function":"worker"}},
      {"ph":"X","pid":1,"ts":20,"dur":40,"name":"work","cat":"app","args":{"caller_function":"worker"}},
      {"ph":"i","pid":2,"ts":30,"name":"request","cat":"heap","args":{"requested_bytes":40,"failed":true,"caller_function":"capture"}},
      {"ph":"i","pid":2,"ts":40,"name":"request","cat":"heap","args":{"requested_bytes":60,"caller_function":"capture"}},
      {"ph":"i","pid":2,"ts":40,"name":"request","cat":"heap","args":{"requested_bytes":0,"failed":false,"caller_function":"capture"}},
      {"ph":"i","pid":2,"ts":40,"name":"allocated","cat":"heap,allocation","args":{"bytes":104,"caller_function":"capture"}}
    ]"#).unwrap();
    let selected = TimingRange {
        start_ns: 30_000,
        end_ns: 50_000,
    };
    let groups = trace.source_stats(selected, "", 0);
    let work = groups.iter().find(|group| group.name == "work").unwrap();
    assert_eq!(work.interval_count, 2);
    assert_eq!(work.inclusive_ns, 40_000);
    assert_eq!(work.longest_overlap_ns, 20_000);
    let request = groups.iter().find(|group| group.name == "request").unwrap();
    assert_eq!((request.request_count, request.requested_bytes), (3, 100));
    assert_eq!((request.failed_requests, request.unknown_outcomes), (1, 1));
    let allocation = groups
        .iter()
        .find(|group| group.name == "allocated")
        .unwrap();
    assert_eq!(allocation.request_count, 0);
    let filtered = trace.source_stats(selected, "", 50_000);
    assert_eq!(
        filtered
            .iter()
            .find(|group| group.name == "work")
            .unwrap()
            .interval_count,
        1
    );
    assert_eq!(
        filtered
            .iter()
            .find(|group| group.name == "request")
            .unwrap()
            .request_count,
        3
    );
    assert!(trace
        .source_stats(
            TimingRange {
                start_ns: 80_000,
                end_ns: 20_000
            },
            "",
            0
        )
        .is_empty());
}

#[test]
fn paired_heap_outcomes_use_terminal_evidence_and_keep_beginning_source_identity() {
    let trace = parse_timing(r#"[
      {"ph":"B","pid":1,"ts":0,"name":"request","cat":"heap","args":{"requested_bytes":64,"caller_function":"capture::open","failed":false}},
      {"ph":"E","pid":1,"ts":1,"args":{"failed":true,"caller_function":"allocator_end","requested_bytes":4096}},
      {"ph":"B","pid":1,"ts":2,"name":"request","cat":"heap","args":{"requested_bytes":32,"caller_function":"capture::open"}},
      {"ph":"E","pid":1,"ts":3,"args":{"failed":false}},
      {"ph":"B","pid":1,"ts":4,"name":"request","cat":"heap","args":{"requested_bytes":16,"caller_function":"capture::open"}},
      {"ph":"E","pid":1,"ts":5,"args":{"outcome":"unknown"}}
    ]"#).unwrap();
    assert_eq!(trace.events[0].request_failed(), Some(true));
    assert_eq!(trace.events[1].request_failed(), Some(false));
    assert_eq!(trace.events[2].request_failed(), None);
    assert_eq!(trace.events[0].source_name(), Some("capture::open"));
    assert_eq!(trace.events[0].requested_bytes(), Some(64));
    let groups = trace.source_stats(trace.bounds, "cat:heap", 0);
    assert_eq!(groups.len(), 1);
    assert_eq!(groups[0].source.as_deref(), Some("capture::open"));
    assert_eq!(groups[0].request_count, 3);
    assert_eq!(groups[0].requested_bytes, 112);
    assert_eq!(groups[0].failed_requests, 1);
    assert_eq!(groups[0].unknown_outcomes, 1);
}

#[test]
fn native_counts_remain_distinct_from_derived_events_and_reject_malformed_counts() {
    let source = r#"{"otherData":{"nxrs":{"event_counts":{"irq_enter":3,"irq_leave":2,"heap_request":4},"source_attribution":{"callers_resolved":2}}},"traceEvents":[{"ph":"i","pid":1,"ts":0,"name":"one display marker"}]}"#;
    let trace = parse_timing(source).unwrap();
    assert_eq!(trace.events.len(), 1);
    assert_eq!(trace.metadata.event_counts["heap_request"], 4);
    assert_eq!(trace.metadata.source_attribution["callers_resolved"], 2);
    assert!(parse_timing(&source.replace("\"irq_enter\":3", "\"irq_enter\":-1")).is_err());
    assert!(parse_timing(&source.replace("\"irq_enter\":3", "\"irq_enter\":1.5")).is_err());
}

#[test]
fn callback_sources_and_attribution_elf_identity_are_preserved() {
    let hash = "a".repeat(64);
    let source = format!(
        r#"{{"otherData":{{"nxrs":{{"elf_sha256":"{hash}","observed_note_count":4,"ignored_line_count":2,"source_attribution":{{"elf_sha256":"{hash}","handler_resolved":1}}}}}},"traceEvents":[{{"ph":"X","pid":1,"ts":0,"dur":1,"name":"timer callback","cat":"timer","args":{{"handler_function":"imu_wake_hint"}}}}]}}"#
    );
    let trace = parse_timing(&source).unwrap();
    assert!(trace.events[0].is_callback());
    assert!(!trace.events[0].is_irq());
    assert_eq!(trace.events[0].source_name(), Some("imu_wake_hint"));
    assert_eq!(
        trace.metadata.source_elf_sha256.as_deref(),
        Some(hash.as_str())
    );
    assert_eq!(trace.metadata.observed_note_count, Some(4));
    assert_eq!(trace.metadata.ignored_line_count, Some(2));
    let mismatch = source.replacen(
        &format!("\"source_attribution\":{{\"elf_sha256\":\"{hash}\""),
        &format!(
            "\"source_attribution\":{{\"elf_sha256\":\"{}\"",
            "b".repeat(64)
        ),
        1,
    );
    assert!(parse_timing(&mismatch)
        .unwrap_err()
        .to_string()
        .contains("source attribution ELF"));
}

#[test]
fn category_filter_ignores_incidental_argument_names_and_keeps_counter_categories() {
    let trace = parse_timing(r#"[
        {"ph":"X","pid":1,"ts":0,"dur":10,"name":"worker","cat":"sched,cpu","args":{"includes_irq_time":true}},
        {"ph":"X","pid":2,"ts":1,"dur":2,"name":"IRQ 15","cat":"irq"},
        {"ph":"C","pid":3,"ts":2,"name":"heap bytes","cat":"heap,usage","args":{"used":128}}
    ]"#).unwrap();
    assert_eq!(
        trace.query(trace.bounds, "irq").len(),
        2,
        "free text continues to search recorded arguments"
    );
    let irqs = trace.query(trace.bounds, "cat:irq");
    assert_eq!(irqs.len(), 1);
    assert_eq!(irqs[0].name, "IRQ 15");
    assert_eq!(trace.counter_query(trace.bounds, "cat:heap").len(), 1);
    assert_eq!(trace.counter_query(trace.bounds, "category:usage").len(), 1);
    assert_eq!(trace.query(trace.bounds, "category:SCHED").len(), 1);
    assert!(trace.query(trace.bounds, "cat:").is_empty());
}
