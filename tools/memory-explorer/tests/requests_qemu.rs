//! Compare actual native request/result notes with independent call-return rows
//! and the imported viewer events. Never derive the oracle from the converter.

use nxrs_memory_explorer::{load_inputs, load_timing, TimingEventKind};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

fn json(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn sha256(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}

fn number(text: &str) -> u64 {
    text.strip_prefix("0x").map_or_else(
        || text.parse().unwrap(),
        |value| u64::from_str_radix(value, 16).unwrap(),
    )
}

fn fields(text: &str) -> BTreeMap<String, String> {
    let mut result = BTreeMap::new();
    for word in text.split_whitespace() {
        let (key, value) = word.split_once('=').unwrap();
        assert!(result.insert(key.to_owned(), value.to_owned()).is_none());
    }
    result
}

fn frame(console: &str, prefix: &str) -> BTreeMap<String, String> {
    let rows: Vec<_> = console
        .lines()
        .filter_map(|line| line.strip_prefix(prefix))
        .collect();
    assert_eq!(rows.len(), 1, "exactly one native {prefix} row");
    fields(rows[0])
}

#[derive(Debug)]
struct Edge {
    kind: String,
    operation: String,
    time: u64,
    task: u64,
    values: BTreeMap<String, String>,
}

fn edges(console: &str) -> Vec<Edge> {
    let mut inside = false;
    let mut last_time = 0;
    let mut result = Vec::new();
    for line in console.lines() {
        if line.starts_with("NXRS_NOTE_BEGIN ") {
            assert!(!inside);
            inside = true;
            continue;
        }
        if line.starts_with("NXRS_NOTE_END ") {
            assert!(inside);
            inside = false;
            continue;
        }
        if !inside {
            continue;
        }
        let (task, record) = line.split_once("[0]").unwrap();
        let task = number(task.trim().rsplit_once('-').unwrap().1);
        let (stamp, event) = record.trim().split_once(": ").unwrap();
        let (seconds, nanos) = stamp.split_once('.').unwrap();
        assert_eq!(nanos.len(), 9);
        let time = number(seconds) * 1_000_000_000 + number(nanos);
        assert!(last_time <= time && time < 5_000_000_000);
        last_time = time;
        let (kind, payload) = event.split_once(": ").unwrap();
        if !matches!(kind, "heap_request" | "heap_result") {
            continue;
        }
        let mut words = payload.split_whitespace();
        let operation = words.next().unwrap().to_owned();
        assert!(matches!(
            operation.as_str(),
            "malloc" | "memalign" | "realloc"
        ));
        let mut values = BTreeMap::new();
        while let Some(word) = words.next() {
            let (key, value) = word.split_once(':').unwrap();
            let value = if value.is_empty() {
                words.next().unwrap()
            } else {
                value
            };
            assert!(values.insert(key.to_owned(), value.to_owned()).is_none());
        }
        result.push(Edge {
            kind: kind.to_owned(),
            operation,
            time,
            task,
            values,
        });
    }
    assert!(!inside);
    result
}

#[test]
#[ignore = "requires fresh native allocator outcome build/capture via NXRS_HEAP_REQUEST_* env"]
fn actual_native_calls_match_return_rows_and_imported_outcomes() {
    let build_dir = PathBuf::from(std::env::var_os("NXRS_HEAP_REQUEST_BUILD").unwrap());
    let capture_dir = PathBuf::from(std::env::var_os("NXRS_HEAP_REQUEST_EVIDENCE").unwrap());
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let build = json(&build_dir.join("build.json"));
    let report = json(&capture_dir.join("report.json"));
    let console = fs::read_to_string(capture_dir.join("console.log")).unwrap();
    assert!(!console.contains("NXRS_HEAP_REQUESTS FAIL"));
    assert_eq!(
        build["nuttx_revision"],
        "2b5c0de5e933281c209f954a63b99cf10c132346"
    );
    assert_eq!(
        build["apps_revision"],
        "3c772ea35b9245972e18999828f7b533e6fc57cc"
    );
    for source in ["nuttx", "apps"] {
        assert_eq!(
            build["source_identity_before_patch"][source]["tracked_sources_clean"],
            true
        );
    }
    let patch = "upstream/nuttx/patches/0148-trace-heap-allocation-requests.patch";
    assert_eq!(build["production_patch"]["path"], patch);
    assert_eq!(
        build["production_patch"]["sha256"],
        sha256(&repo.join(patch))
    );
    let source_hashes = &build["production_patch"]["original_sources"];
    for (path, hash) in [
        (
            "include/nuttx/sched_note.h",
            "6a1bb6368e09f3dfbab7de967ea7694ccbb7a2ff25e7473a849195c50a99b168",
        ),
        (
            "drivers/note/note_driver.c",
            "e7a059a2e5f11d87c494c2d87a63df5e03fa9d7a1fc12fe5a7a99b66427aa6a6",
        ),
        (
            "drivers/note/noteram_driver.c",
            "090ccc10efc40fea992f6b40cfd2b37a94aea69ebb46488c1d3ede43a2ad7e78",
        ),
    ] {
        assert_eq!(source_hashes[path], hash, "pinned note ABI/source differs");
        assert_eq!(
            build["production_patch"]["patched_sources"][path]
                .as_str()
                .unwrap()
                .len(),
            64
        );
    }
    let fixture = repo.join("tests/nuttx-std/heap-requests");
    assert_eq!(build["fixture_sha256"].as_object().unwrap().len(), 4);
    for name in ["Kconfig", "Make.defs", "Makefile", "requests_main.c"] {
        assert_eq!(build["fixture_sha256"][name], sha256(&fixture.join(name)));
    }
    assert_eq!(
        build["shared_clock_sha256"],
        sha256(&repo.join("tests/nuttx-std/memory-timing/qemu_timer_clock.c"))
    );
    for symbol in [
        "MM_ALLOC_REQUEST_TRACE",
        "SCHED_INSTRUMENTATION_HEAP",
        "DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE",
    ] {
        assert_eq!(build["configuration_checks"][symbol]["effective"], "y");
    }
    assert_eq!(build["configuration_checks"]["SMP"]["effective"], "n");
    let config_sha = sha256(&build_dir.join("resolved.config"));
    assert_eq!(build["config_sha256"], config_sha);
    assert_eq!(report["config_sha256"], config_sha);
    assert_eq!(sha256(&capture_dir.join("resolved.config")), config_sha);
    assert_eq!(
        report["build_sha256"],
        sha256(&build_dir.join("build.json"))
    );
    let elf = build_dir.join("nuttx");
    let model = load_inputs(&elf, None, &[], None).unwrap();
    assert_eq!(build["elf_sha256"], sha256(&elf));
    assert_eq!(report["elf_sha256"], model.build.elf_sha256);
    for entry in build["linked_recorder_symbols"].as_array().unwrap() {
        let symbol = model
            .symbols
            .iter()
            .find(|symbol| symbol.name == entry["name"].as_str().unwrap())
            .unwrap();
        assert_eq!(entry["address"].as_u64().unwrap() & !1, symbol.start & !1);
        assert_eq!(entry["size"].as_u64().unwrap(), symbol.size);
        if symbol.name == "g_mm_request_id" {
            assert_eq!(symbol.size, 8);
        }
    }
    assert_eq!(
        build["linked_recorder_symbols"].as_array().unwrap().len(),
        6
    );
    assert_eq!(build["note_buffer_bytes"], 32768);

    let begin = frame(&console, "NXRS_NOTE_BEGIN ");
    let end = frame(&console, "NXRS_NOTE_END ");
    let summary = frame(&console, "NXRS_HEAP_REQUESTS PASS ");
    let recording = &summary["recording_id"];
    assert_eq!(recording.len(), 32);
    assert_eq!(&begin["recording_id"], recording);
    assert_eq!(&end["recording_id"], recording);
    assert_eq!(report["recording_id"].as_str().unwrap(), recording);
    assert_eq!(
        &json(&capture_dir.join("run.json"))["recording_id"],
        &report["recording_id"]
    );
    for fields in [&begin, &end] {
        assert_eq!(fields["mode"], "0");
        assert_eq!(fields["notes_lost"], "0");
    }
    assert_eq!(begin["clock"], "qemu-an521-cmsdk-timer1-20mhz");
    assert_eq!(begin["frequency_hz"], "20000000");
    assert_eq!(end["complete"], "1");
    assert_eq!(summary["rows"], "12");
    for key in [
        "request_note_bytes",
        "result_note_bytes",
        "fixture_rows_bytes",
    ] {
        assert_eq!(report["target_reported_sizes"][key], number(&summary[key]));
    }
    assert!((1..256).contains(&number(&summary["request_note_bytes"])));
    assert!((1..256).contains(&number(&summary["result_note_bytes"])));

    let heap = number(&summary["heap"]);
    let owner = number(&summary["owner"]);
    let raw = edges(&console);
    let mut requests = BTreeMap::new();
    let mut results = BTreeMap::new();
    let mut stack = Vec::new();
    for edge in raw
        .iter()
        .filter(|edge| number(&edge.values["heap"]) == heap)
    {
        assert_eq!(edge.task, owner);
        let id = number(&edge.values["id"]);
        assert_ne!(id, 0);
        if edge.kind == "heap_request" {
            assert!(requests.insert(id, edge).is_none());
            stack.push(id);
        } else {
            assert_eq!(stack.pop(), Some(id));
            assert!(results.insert(id, edge).is_none());
            let request = requests[&id];
            assert_eq!(request.operation, edge.operation);
            assert!(request.time <= edge.time);
            assert_eq!(
                number(&edge.values["result"]) == 0,
                edge.values["outcome"] == "failure"
            );
        }
    }
    assert!(stack.is_empty());
    assert_eq!(
        requests.keys().collect::<Vec<_>>(),
        results.keys().collect::<Vec<_>>()
    );
    assert_eq!(report["calls"], requests.len());
    let trace = load_timing(&capture_dir.join("timing.json")).unwrap();
    assert_eq!(
        trace.metadata.elf_sha256.as_deref(),
        Some(model.build.elf_sha256.as_str())
    );
    assert_eq!(
        trace.metadata.recording_id.as_deref(),
        Some(recording.as_str())
    );
    assert_eq!(
        trace.metadata.clock.as_deref(),
        Some(begin["clock"].as_str())
    );
    let origin = trace.metadata.origin_ns.unwrap();
    let converted_calls: Vec<_> = trace
        .events
        .iter()
        .filter(|event| {
            event
                .argument("heap")
                .and_then(Value::as_str)
                .is_some_and(|value| number(value) == heap)
        })
        .filter_map(|event| {
            event
                .argument("request_id")
                .and_then(Value::as_str)
                .map(|id| (number(id), event))
        })
        .collect();
    let calls: BTreeMap<_, _> = converted_calls.iter().copied().collect();
    assert_eq!(
        calls.len(),
        converted_calls.len(),
        "duplicate imported call identity"
    );
    assert_eq!(calls.len(), requests.len());
    for (id, request) in &requests {
        let result = results[id];
        let event = calls[id];
        assert_eq!(event.kind, TimingEventKind::Interval);
        assert_eq!(event.name, request.operation);
        assert_eq!(event.start_ns + origin, request.time);
        assert_eq!(event.end_ns + origin, result.time);
        for (arg, native) in [("requested_bytes", "size"), ("alignment", "align")] {
            assert_eq!(
                event.argument(arg).and_then(Value::as_u64),
                Some(number(&request.values[native]))
            );
        }
        for (arg, native) in [("caller", "caller"), ("old_address", "old")] {
            assert_eq!(
                number(event.argument(arg).and_then(Value::as_str).unwrap()),
                number(&request.values[native])
            );
        }
        assert_eq!(
            number(
                event
                    .argument("result_address")
                    .and_then(Value::as_str)
                    .unwrap()
            ),
            number(&result.values["result"])
        );
        let failed = result.values["outcome"] == "failure";
        assert_eq!(
            event.argument("outcome").and_then(Value::as_str),
            Some(if failed { "failed" } else { "succeeded" })
        );
        assert_eq!(
            event.argument("failed").and_then(Value::as_bool),
            Some(failed)
        );
        assert_eq!(
            event.argument("request_observed").and_then(Value::as_bool),
            Some(true)
        );
    }

    let caller = model
        .symbols
        .iter()
        .find(|symbol| symbol.name == "requests_observed_call")
        .unwrap();
    let rows: Vec<_> = console
        .lines()
        .filter_map(|line| line.strip_prefix("NXRS_REQUEST_ROW "))
        .map(fields)
        .collect();
    assert_eq!(rows.len(), 12);
    let mut seen = BTreeSet::new();
    for (index, row) in rows.iter().enumerate() {
        assert_eq!(number(&row["index"]), index as u64);
        assert_eq!(row["payload_preserved"], "1");
        let first = number(&row["before_ticks"]) * 50;
        let last = number(&row["after_ticks"]) * 50;
        let mut observed: Vec<_> = requests
            .iter()
            .filter(|(_, edge)| (first..=last).contains(&edge.time))
            .collect();
        observed.sort_by_key(|(_, edge)| edge.time);
        if row["phase"] != "enabled" {
            assert!(
                observed.is_empty(),
                "runtime-disabled allocator call emitted notes"
            );
            continue;
        }
        assert_eq!(
            observed.len() as u64,
            number(&row["after_count"]) - number(&row["before_count"])
        );
        let (id, request) = observed[0];
        assert_eq!(request.operation, row["op"]);
        for key in ["size", "old", "align"] {
            assert_eq!(number(&request.values[key]), number(&row[key]));
        }
        assert_eq!(
            number(&results[id].values["result"]),
            number(&row["result"])
        );
        let pc = number(&request.values["caller"]) & !1;
        assert!((caller.start & !1) <= pc && pc < (caller.start & !1) + caller.size);
        for (id, _) in observed {
            assert!(results[id].time <= last);
            assert!(seen.insert(*id));
        }
    }
    assert_eq!(seen, requests.keys().copied().collect());

    let removed = report["controls"]["missing_result"]["request_id"]
        .as_u64()
        .unwrap()
        .to_string();
    for name in ["missing_result", "missing_request", "missing_pair"] {
        let control = load_timing(&capture_dir.join(format!("{name}.json"))).unwrap();
        assert_eq!(
            control.metadata.recording_id.as_deref(),
            Some(recording.as_str())
        );
        let affected: Vec<_> = control
            .events
            .iter()
            .filter(|event| {
                event.argument("request_id").and_then(Value::as_str) == Some(removed.as_str())
            })
            .collect();
        assert!(!report["controls"][name]["rejected_by_native_oracle"]
            .as_str()
            .unwrap()
            .is_empty());
        if name == "missing_pair" {
            assert!(affected.is_empty());
            continue;
        }
        assert_eq!(affected.len(), 1);
        let event = affected[0];
        assert_eq!(event.kind, TimingEventKind::Instant);
        assert_eq!(
            event.argument("duration_unknown").and_then(Value::as_bool),
            Some(true)
        );
        assert!(event.argument("outcome").is_none());
        assert!(event.argument("result_address").is_none());
        if name == "missing_request" {
            assert_eq!(
                event.argument("request_observed").and_then(Value::as_bool),
                Some(false)
            );
            assert!(event.argument("requested_bytes").is_none());
            assert_eq!(
                event.argument("observed_outcome").and_then(Value::as_str),
                Some("succeeded")
            );
        }
    }
    println!("Independent native allocator gate passed: {} calls, actual return rows, nested IDs, exact source/ELF identity, disabled filtering and three missing-edge controls.", requests.len());
}
