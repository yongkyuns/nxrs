use nxrs_memory_explorer::{
    parse_timing, TimingRange, TimingRateDenominator, TimingRateUnavailable,
};
use serde_json::{json, Value};

const SECOND: u64 = 1_000_000_000;
const ORIGIN: u64 = 9_007_199_254_740_993;

fn document(events: Value) -> Value {
    json!({
        "otherData": {"nxrs": {
            "schema_version": 1,
            "recording_id": "timed-window-test",
            "clock": "fixture-monotonic",
            "origin_ns": ORIGIN,
            "loss_detected": false,
            "complete": true,
            "capture_window": {
                "start_ns": ORIGIN,
                "end_ns": ORIGIN + 3 * SECOND,
                "requested_duration_ns": 3 * SECOND,
                "stop_reason": "duration",
                "complete": true,
                "categories": ["app", "irq"]
            }
        }},
        "traceEvents": events
    })
}

fn range(start: u64, end: u64) -> TimingRange {
    TimingRange {
        start_ns: start,
        end_ns: end,
    }
}

#[test]
fn silence_keeps_the_explicit_capture_and_has_a_known_zero_count() {
    for events in [
        json!([]),
        json!([{"ph":"M","pid":1,"name":"process_name","args":{"name":"quiet firmware"}}]),
    ] {
        let trace = parse_timing(&document(events).to_string()).unwrap();
        assert_eq!(trace.bounds, range(0, 3 * SECOND));
        assert_eq!(trace.capture_range(), Some(trace.bounds));
        assert!(trace.events.is_empty());
        assert!(trace.counters.is_empty());
        let stats = trace.stats(trace.bounds, "cat:app");
        assert_eq!(stats.occurrences.interval_starts, 0);
        assert_eq!(stats.occurrences.instants, 0);
        assert_eq!(stats.rate_denominator.duration_ns(), Some(3 * SECOND));
        assert_eq!(stats.rate_denominator.per_second(0), Some(0.0));
        assert_eq!(stats.occurrences.mean_duration_ns(), None);
    }
    assert!(parse_timing(r#"{"traceEvents":[]}"#).is_err());
}

#[test]
fn sparse_observations_do_not_trim_silence_or_use_requested_duration_for_rates() {
    let mut input = document(json!([
        {"ph":"i","pid":1,"name":"fix_received","cat":"app","ts":1_000_000},
        {"ph":"i","pid":1,"name":"fix_received","cat":"app","ts":2_000_000}
    ]));
    input["otherData"]["nxrs"]["capture_window"]["requested_duration_ns"] = json!(2 * SECOND);
    let trace = parse_timing(&input.to_string()).unwrap();
    assert_eq!(trace.bounds, range(0, 3 * SECOND));
    assert_eq!(
        trace.metadata.capture_window.as_ref().unwrap().start_ns,
        ORIGIN
    );
    let stats = trace.stats(trace.bounds, "fix_received");
    assert_eq!(stats.occurrences.instants, 2);
    assert_eq!(stats.rate_denominator.per_second(2), Some(2.0 / 3.0));
}

#[test]
fn adjacent_buckets_assign_instants_and_complete_starts_once() {
    let trace = parse_timing(
        &document(json!([
            {"ph":"i","pid":1,"name":"tick","cat":"app","ts":0},
            {"ph":"i","pid":1,"name":"tick","cat":"app","ts":1_000_000},
            {"ph":"i","pid":1,"name":"tick","cat":"app","ts":2_000_000},
            {"ph":"X","pid":2,"name":"batch","cat":"app","ts":250_000,"dur":1_500_000},
            {"ph":"X","pid":2,"name":"zero duration","cat":"app","ts":2_000_000,"dur":0}
        ]))
        .to_string(),
    )
    .unwrap();
    let first = trace.stats(range(0, SECOND), "");
    let second = trace.stats(range(SECOND, 2 * SECOND), "");
    let third = trace.stats(range(2 * SECOND, 3 * SECOND), "");
    assert_eq!(first.occurrences.instants, 1);
    assert_eq!(second.occurrences.instants, 1);
    assert_eq!(third.occurrences.instants, 1);
    assert_eq!(first.occurrences.interval_starts, 1);
    assert_eq!(second.occurrences.interval_starts, 0);
    assert_eq!(third.occurrences.interval_starts, 1);
    assert_eq!(first.clipped_interval_sum_ns, 750_000_000);
    assert_eq!(second.clipped_interval_sum_ns, 750_000_000);
    assert_eq!(first.occurrences.full_duration_sum_ns, 1_500_000_000);
    assert_eq!(first.occurrences.mean_duration_ns(), Some(1_500_000_000.0));
    assert_eq!(second.occurrences.mean_duration_ns(), None);
    assert_eq!(third.occurrences.mean_duration_ns(), Some(0.0));
    assert_eq!(third.occurrences.longest_duration_ns, Some(0));
    // Display queries still include a marker on the selected right edge.
    assert_eq!(first.instant_count, 2);
}

#[test]
fn full_call_latency_is_not_its_clipped_overlap() {
    let trace = parse_timing(
        &document(json!([
            {"ph":"X","pid":1,"name":"work","cat":"app","ts":500_000,"dur":2_000_000,
             "args":{"caller_function":"worker"}}
        ]))
        .to_string(),
    )
    .unwrap();
    let starting = trace.source_stats(range(0, SECOND), "work", 0);
    assert_eq!(starting[0].inclusive_ns, 500_000_000);
    assert_eq!(starting[0].longest_overlap_ns, 500_000_000);
    assert_eq!(starting[0].occurrences.complete_intervals, 1);
    assert_eq!(
        starting[0].occurrences.mean_duration_ns(),
        Some(2_000_000_000.0)
    );
    assert_eq!(
        starting[0].occurrences.longest_duration_ns,
        Some(2 * SECOND)
    );
    let middle = trace.source_stats(range(SECOND, 2 * SECOND), "work", 0);
    assert_eq!(middle[0].inclusive_ns, u128::from(SECOND));
    assert_eq!(middle[0].occurrences.interval_starts, 0);
    assert_eq!(middle[0].occurrences.mean_duration_ns(), None);
}

#[test]
fn edge_markers_do_not_disable_a_complete_retained_window_or_become_occurrences() {
    let mut input = document(json!([
        {"ph":"i","pid":1,"name":"unclosed IRQ","cat":"irq,unknown,capture","ts":0,
         "args":{"duration_unknown":true,"reason":"capture edge"}},
        {"ph":"i","pid":1,"name":"unknown scope","cat":"app","ts":1_000_000,
         "args":{"reason":"missing beginning"}},
        {"ph":"i","pid":1,"name":"unknown duration","cat":"app","ts":1_500_000,
         "args":{"duration_unknown":true}},
        {"ph":"i","pid":1,"name":"fix_received","cat":"app","ts":2_000_000},
        {"ph":"X","pid":2,"name":"gnss/batch","cat":"app","ts":2_100_000,"dur":100_000}
    ]));
    input["otherData"]["nxrs"]["complete"] = json!(false);
    input["otherData"]["nxrs"]["incomplete_intervals"] = json!(3);
    let trace = parse_timing(&input.to_string()).unwrap();
    let stats = trace.stats(trace.bounds, "");
    assert_eq!(stats.occurrences.excluded_markers, 3);
    assert_eq!(stats.occurrences.instants, 1);
    assert_eq!(stats.occurrences.interval_starts, 1);
    assert_eq!(stats.rate_denominator.duration_ns(), Some(3 * SECOND));
    assert_eq!(trace.metadata.complete, Some(false));
    assert!(trace.metadata.capture_window.as_ref().unwrap().complete);
    let batch = trace.source_stats(trace.bounds, "gnss/batch", 0);
    assert_eq!(batch[0].name, "gnss/batch");
    assert_eq!(batch[0].occurrences.interval_starts, 1);
    assert_eq!(batch[0].occurrences.instants, 0);
}

#[test]
fn absent_lost_or_incomplete_coverage_keeps_rates_unknown() {
    let legacy = parse_timing(r#"[{"ph":"X","pid":1,"name":"legacy","ts":0,"dur":1}]"#).unwrap();
    assert_eq!(
        legacy.rate_denominator(legacy.bounds),
        TimingRateDenominator::Unavailable(TimingRateUnavailable::NoCaptureWindow)
    );
    for (loss, complete, expected) in [
        (None, true, TimingRateUnavailable::UnknownLoss),
        (Some(false), false, TimingRateUnavailable::IncompleteCapture),
        (Some(true), false, TimingRateUnavailable::RecordLoss),
    ] {
        let mut input = document(json!([]));
        input["otherData"]["nxrs"]["loss_detected"] = json!(loss);
        input["otherData"]["nxrs"]["capture_window"]["complete"] = json!(complete);
        let trace = parse_timing(&input.to_string()).unwrap();
        let rate = trace.rate_denominator(trace.bounds);
        assert_eq!(rate, TimingRateDenominator::Unavailable(expected));
        assert_eq!(rate.per_second(0), None);
    }
    let mut zero_lost = document(json!([]));
    zero_lost["otherData"]["nxrs"]["loss_detected"] = Value::Null;
    zero_lost["otherData"]["nxrs"]["lost_records"] = json!(0);
    let trace = parse_timing(&zero_lost.to_string()).unwrap();
    assert_eq!(
        trace.rate_denominator(trace.bounds).per_second(0),
        Some(0.0)
    );
    assert_eq!(
        trace.rate_denominator(range(SECOND, SECOND)),
        TimingRateDenominator::Unavailable(TimingRateUnavailable::EmptyWindow)
    );
    assert_eq!(
        trace.rate_denominator(range(0, 4 * SECOND)),
        TimingRateDenominator::Unavailable(TimingRateUnavailable::OutsideCapture)
    );
}

#[test]
fn interrupted_window_uses_its_retained_actual_duration() {
    let mut input = document(json!([]));
    let window = &mut input["otherData"]["nxrs"]["capture_window"];
    window["stop_reason"] = json!("interrupted");
    window["requested_duration_ns"] = json!(10 * SECOND);
    window["categories"] = json!([]);
    let trace = parse_timing(&input.to_string()).unwrap();
    assert_eq!(trace.bounds, range(0, 3 * SECOND));
    assert_eq!(
        trace.rate_denominator(trace.bounds).duration_ns(),
        Some(3 * SECOND)
    );
}

#[test]
fn malformed_or_contradictory_window_metadata_is_rejected() {
    for (key, value) in [
        ("start_ns", json!(ORIGIN + 3 * SECOND)),
        ("end_ns", json!(ORIGIN)),
        ("requested_duration_ns", json!(0)),
        ("requested_duration_ns", json!(4 * SECOND)),
        ("requested_duration_ns", json!(1.5)),
        ("start_ns", json!(-1)),
        ("end_ns", json!("12000000000")),
        ("complete", Value::Null),
        ("complete", json!(1)),
        ("stop_reason", json!("guessed")),
        ("categories", json!(["app", "app"])),
        ("categories", json!(["gnss"])),
        ("categories", json!("app")),
        ("unexpected", json!(true)),
    ] {
        let mut input = document(json!([]));
        input["otherData"]["nxrs"]["capture_window"][key] = value;
        assert!(
            parse_timing(&input.to_string()).is_err(),
            "accepted invalid {key}: {input}"
        );
    }
    for (key, value) in [
        ("origin_ns", Value::Null),
        ("origin_ns", json!(ORIGIN - 1)),
        ("recording_id", Value::Null),
        ("clock", Value::Null),
        ("clock", json!("  ")),
        ("loss_detected", json!(true)),
        ("lost_records", json!(1)),
    ] {
        let mut input = document(json!([]));
        input["otherData"]["nxrs"][key] = value;
        assert!(
            parse_timing(&input.to_string()).is_err(),
            "accepted contradictory {key}: {input}"
        );
    }
}

#[test]
fn observations_cannot_extend_the_window_and_right_edge_instants_are_excluded() {
    for event in [
        json!({"ph":"i","pid":1,"name":"right edge","ts":3_000_000}),
        json!({"ph":"X","pid":1,"name":"past end","ts":2_000_000,"dur":1_000_001}),
        json!({"ph":"C","pid":1,"name":"right counter","ts":3_000_000,"args":{"value":1}}),
    ] {
        assert!(parse_timing(&document(json!([event])).to_string()).is_err());
    }
    let trace = parse_timing(
        &document(json!([
            {"ph":"X","pid":1,"name":"ends at boundary","ts":2_000_000,"dur":1_000_000}
        ]))
        .to_string(),
    )
    .unwrap();
    assert_eq!(
        trace.stats(trace.bounds, "").occurrences.complete_intervals,
        1
    );
}
