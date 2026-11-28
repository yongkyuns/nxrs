//! Compare production finite captures with native bytes and independent work.

use nxrs_memory_explorer::{load_inputs, load_timing, TimingEventKind, TimingRange};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

mod support;
use support::source_pins;

type Fields = BTreeMap<String, String>;

fn json(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn sha256(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}

fn fields(text: &str) -> Fields {
    let mut fields = Fields::new();
    for item in text.split_whitespace() {
        let (key, value) = item.split_once('=').unwrap();
        assert!(fields.insert(key.into(), value.into()).is_none());
    }
    fields
}

fn rows(console: &str, prefix: &str) -> Vec<Fields> {
    let prefix = format!("{prefix} ");
    console
        .lines()
        .filter_map(|line| line.strip_prefix(&prefix).map(fields))
        .collect()
}

fn one(console: &str, prefix: &str) -> Fields {
    let mut rows = rows(console, prefix);
    assert_eq!(rows.len(), 1, "one {prefix} native receipt");
    rows.remove(0)
}

fn number(row: &Fields, key: &str) -> u64 {
    row[key].parse().unwrap()
}

fn hex_bytes(text: &str) -> Vec<u8> {
    assert_eq!(text.len() % 2, 0);
    let (pairs, _) = text.as_bytes().as_chunks::<2>();
    pairs
        .iter()
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect()
}

fn fnv(bytes: impl IntoIterator<Item = u8>) -> u32 {
    bytes.into_iter().fold(2_166_136_261_u32, |sum, byte| {
        (sum ^ u32::from(byte)).wrapping_mul(16_777_619)
    })
}

#[derive(Debug, PartialEq, Eq)]
struct NativeNote {
    time_ns: u64,
    task: u64,
    edge: char,
    label: String,
}

fn raw_notes(console: &str) -> Vec<NativeNote> {
    let abi = one(console, "NXRS_TIMED_ABI");
    for (key, expected) in [
        ("common_bytes", 16),
        ("clock_offset", 8),
        ("clock_bytes", 8),
        ("pid_offset", 4),
        ("pid_bytes", 4),
        ("event_bytes", 32),
        ("data_offset", 24),
        ("begin_type", 31),
        ("end_type", 32),
        ("mark_type", 33),
        ("word_bytes", 4),
    ] {
        assert_eq!(number(&abi, key), expected, "pinned ARM ABI {key}");
    }
    assert_eq!(abi.len(), 11);
    let mut all_bytes = Vec::new();
    let mut aligned_bytes = 0;
    let mut notes = Vec::new();
    for (index, row) in rows(console, "NXRS_TIMED_RAW").iter().enumerate() {
        assert_eq!(number(row, "index"), index as u64);
        let data = hex_bytes(&row["bytes"]);
        assert!(data.len() >= 32);
        assert_eq!(usize::from(data[0]), data.len());
        let label_size = data.len() - 32;
        let label = std::str::from_utf8(&data[24..24 + label_size]).unwrap();
        assert!(label.is_ascii());
        notes.push(NativeNote {
            time_ns: u64::from_le_bytes(data[8..16].try_into().unwrap()) * 50,
            task: u64::from(u32::from_le_bytes(data[4..8].try_into().unwrap())),
            edge: match data[1] {
                31 => 'B',
                32 => 'E',
                33 => 'I',
                other => panic!("unexpected native sparse note type {other}"),
            },
            label: label.into(),
        });
        aligned_bytes += (data.len() + 3) & !3;
        all_bytes.extend(data);
    }
    let end = one(console, "NXRS_TIMED_RAW_END");
    assert_eq!(number(&end, "records"), notes.len() as u64);
    assert_eq!(number(&end, "aligned_bytes"), aligned_bytes as u64);
    assert_eq!(
        u32::from_str_radix(&end["checksum"], 16).unwrap(),
        fnv(all_bytes)
    );
    assert_eq!(
        number(&end, "mode"),
        0,
        "original mode restored after framing"
    );
    assert!(aligned_bytes < 32768);
    assert!(notes
        .windows(2)
        .all(|pair| pair[0].time_ns <= pair[1].time_ns));
    notes
}

fn ascii_notes(console: &str) -> Vec<NativeNote> {
    let mut framed = false;
    let mut notes = Vec::new();
    for line in console.lines() {
        if line.starts_with("NXRS_NOTE_BEGIN ") {
            assert!(!framed);
            framed = true;
        } else if line.starts_with("NXRS_NOTE_END ") {
            assert!(framed);
            framed = false;
        } else if framed && !line.is_empty() {
            let (task, payload) = line.split_once("[0]").unwrap();
            let task = task.trim().rsplit_once('-').unwrap().1.parse().unwrap();
            let (stamp, marker) = payload.trim().split_once(": tracing_mark_write: ").unwrap();
            let (seconds, fraction) = stamp.split_once('.').unwrap();
            assert_eq!(fraction.len(), 9);
            let mut values = marker.splitn(3, '|');
            let edge = values.next().unwrap();
            assert_eq!(edge.len(), 1);
            assert_eq!(values.next().unwrap().parse::<u64>().unwrap(), task);
            notes.push(NativeNote {
                time_ns: seconds.parse::<u64>().unwrap() * 1_000_000_000
                    + fraction.parse::<u64>().unwrap(),
                task,
                edge: edge.chars().next().unwrap(),
                label: values.next().unwrap().into(),
            });
        }
    }
    assert!(!framed);
    notes
}

fn validate_build(build: &Value, report: &Value, build_dir: &Path, repo: &Path) {
    let pins = source_pins();
    assert_eq!(build["nuttx_revision"], pins["nuttx"]);
    assert_eq!(build["apps_revision"], pins["apps"]);
    for source in ["nuttx", "apps"] {
        assert_eq!(
            build["source_identity_before_patch"][source]["tracked_sources_clean"],
            true
        );
    }
    assert_eq!(build["apps_tracked_sources_restored"], true);
    assert_eq!(
        report["build_sha256"],
        sha256(&build_dir.join("build.json"))
    );
    for (field, file) in [
        ("elf_sha256", "nuttx"),
        ("config_sha256", "resolved.config"),
    ] {
        assert_eq!(build[field], sha256(&build_dir.join(file)));
        assert_eq!(report[field], build[field]);
    }
    let patch = "upstream/nuttx-apps/patches/0007-trace-capture-window.patch";
    assert_eq!(build["production_patch"]["path"], patch);
    assert_eq!(
        build["production_patch"]["sha256"],
        sha256(&repo.join(patch))
    );
    assert_eq!(
        sha256(&build_dir.join("production.patch")),
        sha256(&repo.join(patch))
    );
    let fixture = repo.join("tests/nuttx-std/timed-capture");
    let names: BTreeSet<_> = fs::read_dir(&fixture)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    assert_eq!(
        names,
        BTreeSet::from([
            "Kconfig".into(),
            "Make.defs".into(),
            "Makefile".into(),
            "timed_main.c".into()
        ])
    );
    for name in names {
        let hash = sha256(&fixture.join(&name));
        assert_eq!(build["fixture_sha256"][&name], hash);
        assert_eq!(build["installed_fixture_sha256"][&name], hash);
    }
    for (name, source) in [
        (
            "acquisition_trace.c",
            "shared/acquisition/src/platform/nuttx/ffi/trace.c",
        ),
        (
            "qemu_timer_clock.c",
            "tests/nuttx-std/memory-timing/qemu_timer_clock.c",
        ),
    ] {
        let copied = &build["copied_sources"][name];
        assert_eq!(copied["source"], source);
        assert_eq!(copied["sha256"], sha256(&repo.join(source)));
        assert_eq!(copied["installed_sha256"], copied["sha256"]);
    }
    for (container, expected) in [
        (
            &build["build_sources_sha256"],
            [
                "tools/memory-explorer/tests/build_timed_capture_nuttx.py",
                "tools/memory-explorer/tests/build_timing_nuttx.py",
            ],
        ),
        (
            &report["capture_sources_sha256"],
            [
                "tools/memory-explorer/tests/qualify_timed_capture_qemu.py",
                "tools/nuttx-trace-to-perfetto.py",
            ],
        ),
    ] {
        assert_eq!(
            container
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from(expected)
        );
        for (source, hash) in container.as_object().unwrap() {
            assert_eq!(hash, &Value::String(sha256(&repo.join(source))));
        }
    }
    for (symbol, value) in [
        ("SYSTEM_TRACE_CAPTURE", "y"),
        ("SYSTEM_TRACE_STACKSIZE", "4096"),
        ("DRIVERS_NOTERAM_BUFSIZE", "32768"),
        ("DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE", "y"),
        ("SMP", "n"),
        ("SCHED_INSTRUMENTATION_WDOG", "n"),
        ("EXAMPLES_HEAP_REPLAY", "n"),
        ("EXAMPLES_HEAP_REQUESTS", "n"),
        ("PTHREAD_MUTEX_UNSAFE", "n"),
        ("PTHREAD_MUTEX_BOTH", "y"),
        ("SIG_DEFAULT", "y"),
        ("SIG_SIGKILL_ACTION", "y"),
    ] {
        assert_eq!(build["configuration_checks"][symbol]["effective"], value);
    }
    assert_eq!(build["clock_contract"]["frequency_hz"], 20_000_000);
    let model = load_inputs(&build_dir.join("nuttx"), None, &[], None).unwrap();
    for symbol in [
        "trace_main",
        "timed_capture_main",
        "acquisition_trace_event",
        "__wrap_up_perf_gettime",
    ] {
        assert!(model.symbols.iter().any(|entry| entry.name == symbol));
    }
}

#[test]
#[ignore = "requires NXRS_TIMED_CAPTURE_BUILD and NXRS_TIMED_CAPTURE_EVIDENCE from native build/qualification"]
fn finite_windows_preserve_silence_slow_occurrences_and_overflow_evidence() {
    let build_dir = PathBuf::from(std::env::var_os("NXRS_TIMED_CAPTURE_BUILD").unwrap());
    let evidence = PathBuf::from(std::env::var_os("NXRS_TIMED_CAPTURE_EVIDENCE").unwrap());
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let build = json(&build_dir.join("build.json"));
    let report = json(&evidence.join("report.json"));
    validate_build(&build, &report, &build_dir, &repo);
    assert_eq!(report["debugger_stops"], false);
    let cases = report["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 3);
    for (case, (name, seconds, overflow)) in cases.iter().zip([
        ("two-seconds", 2_u64, false),
        ("ten-seconds", 10, false),
        ("overflow", 2, true),
    ]) {
        assert_eq!(case["name"], name);
        let directory = evidence.join(name);
        for (field, file) in [
            ("console_sha256", "console.log"),
            ("native_sha256", "native.json"),
            ("timing_sha256", "timing.json"),
        ] {
            assert_eq!(case[field], sha256(&directory.join(file)));
        }
        let console = fs::read_to_string(directory.join("console.log")).unwrap();
        assert_eq!(
            console
                .lines()
                .filter(|line| *line == "NXRS_TIMED_WITNESS PASS")
                .count(),
            1
        );
        assert!(!console.contains("NXRS_TIMED_WITNESS FAIL"));
        let begin = one(&console, "NXRS_NOTE_BEGIN");
        let end = one(&console, "NXRS_NOTE_END");
        let oracle = one(&console, "NXRS_TIMED_ORACLE");
        let operations = rows(&console, "NXRS_TIMED_OPERATION");
        let raw = raw_notes(&console);
        assert_eq!(
            raw,
            ascii_notes(&console),
            "production text versus raw native ring"
        );
        for frame in [&begin, &end] {
            assert_eq!(
                frame["recording_id"],
                case["recording_id"].as_str().unwrap()
            );
            assert_eq!(number(frame, "schema"), 3);
            assert_eq!(number(frame, "notes_lost"), u64::from(overflow));
            assert_eq!(number(frame, "mode"), if overflow { 2 } else { 0 });
        }
        assert_eq!(number(&end, "complete"), 1);
        assert_eq!(number(&begin, "frequency_hz"), 20_000_000);
        assert_eq!(number(&oracle, "frequency_hz"), 20_000_000);
        assert_eq!(begin["categories"], "app");
        assert_eq!(begin["stop_reason"], "duration");
        let start = number(&begin, "capture_start_ns");
        let finish = number(&begin, "capture_end_ns");
        let duration = finish - start;
        let requested = seconds * 1_000_000_000;
        assert_eq!(number(&begin, "requested_duration_ns"), requested);
        assert!((requested..=requested + 100_000_000).contains(&duration));
        assert!(number(&oracle, "call_before_ticks") * 50 <= start);
        assert!(number(&oracle, "call_after_ticks") * 50 >= finish);
        assert_eq!(number(&oracle, "worker_error"), 0);
        let status = oracle["capture_status"].parse::<i32>().unwrap();
        assert!(if overflow { status > 0 } else { status == 0 });
        assert_eq!(operations.len() as u64, seconds * 3);
        assert_eq!(number(&oracle, "operation_count"), seconds * 3);
        assert_eq!(
            number(&oracle, "flood_count"),
            if overflow { 2048 } else { 0 }
        );
        if name == "two-seconds" {
            let recovery = one(&console, "NXRS_TIMED_OWNER_RECOVERY");
            assert_eq!(number(&recovery, "signal"), 9);
            assert_eq!(number(&recovery, "owner_exit_status"), 1);
            assert_eq!(number(&recovery, "stop_status"), 0);
            assert_eq!(number(&recovery, "recorder_enabled"), 0);
            assert_ne!(
                number(&recovery, "owner_pid"),
                number(&recovery, "stop_pid")
            );
        }
        let mut identities = BTreeSet::new();
        let mut matched = BTreeSet::new();
        for row in &operations {
            let source = number(row, "source");
            let sequence = number(row, "sequence");
            assert!((1..=2).contains(&source) && (1..=seconds * source).contains(&sequence));
            assert!(identities.insert((source, sequence)));
            let checksum =
                fnv((0..256).map(|index| ((source * 41 + sequence * 17 + index) & 255) as u8));
            assert_eq!(u32::from_str_radix(&row["checksum"], 16).unwrap(), checksum);
            let due = number(&oracle, "enabled_seen_ticks")
                + 4_000_000
                + (sequence - 1) * 20_000_000 / source;
            assert_eq!(number(row, "due_ticks"), due);
            let stamps: Vec<_> = [
                "due_ticks",
                "begin_before_ticks",
                "begin_after_ticks",
                "end_before_ticks",
                "end_after_ticks",
                "fix_before_ticks",
                "fix_after_ticks",
            ]
            .into_iter()
            .map(|key| number(row, key))
            .collect();
            assert!(stamps.windows(2).all(|pair| pair[0] <= pair[1]));
            assert!(stamps[1] - due <= 2_000_000);
            assert!(stamps[1] * 50 >= start + 100_000_000);
            assert!(stamps[6] * 50 <= finish - 100_000_000);
            for (edge, stage, before, after) in [
                (
                    'B',
                    "read_parse_deliver",
                    "begin_before_ticks",
                    "begin_after_ticks",
                ),
                (
                    'E',
                    "read_parse_deliver",
                    "end_before_ticks",
                    "end_after_ticks",
                ),
                ('I', "fix_received", "fix_before_ticks", "fix_after_ticks"),
            ] {
                let label = format!("gnss/simulated_{source}hz/{stage}");
                let observations: Vec<_> = raw
                    .iter()
                    .enumerate()
                    .filter(|(_, note)| {
                        note.edge == edge
                            && note.label == label
                            && (number(row, before) * 50..=number(row, after) * 50)
                                .contains(&note.time_ns)
                    })
                    .collect();
                assert!(observations.len() <= 1, "duplicate operation marker");
                if !overflow {
                    assert_eq!(observations.len(), 1, "missing operation marker");
                }
                for (index, note) in observations {
                    assert!(
                        matched.insert(index),
                        "one marker cannot prove two operations"
                    );
                    assert_eq!(note.task, number(&oracle, "worker_task_id"));
                }
            }
        }
        let expected_ids: BTreeSet<_> = (1..=2)
            .flat_map(|source| (1..=seconds * source).map(move |sequence| (source, sequence)))
            .collect();
        assert_eq!(identities, expected_ids);
        if overflow {
            assert!(matched.len() < operations.len() * 3);
            assert!(32768 - number(&one(&console, "NXRS_TIMED_RAW_END"), "aligned_bytes") <= 256);
        } else {
            assert_eq!(matched.len(), raw.len());
        }
        let trace = load_timing(&directory.join("timing.json")).unwrap();
        let window = trace.metadata.capture_window.as_ref().unwrap();
        assert_eq!(window.start_ns, start);
        assert_eq!(window.end_ns, finish);
        assert_eq!(window.requested_duration_ns, requested);
        assert_eq!(window.complete, !overflow);
        assert_eq!(
            trace.bounds,
            TimingRange {
                start_ns: 0,
                end_ns: duration
            }
        );
        assert_eq!(trace.metadata.origin_ns, Some(start));
        assert_eq!(trace.metadata.loss_detected, Some(overflow));
        assert_eq!(
            trace.metadata.elf_sha256.as_deref(),
            build["elf_sha256"].as_str()
        );
        for source in 1..=2 {
            let fix_name = format!("gnss/simulated_{source}hz/fix_received");
            let scope_name = format!("gnss/simulated_{source}hz/read_parse_deliver");
            let fixes = trace.stats(trace.bounds, &fix_name);
            let fix_sources = trace.source_stats(trace.bounds, &fix_name, 0);
            if overflow {
                assert!(fixes
                    .rate_denominator
                    .per_second(fixes.occurrences.instants)
                    .is_none());
                assert!(fix_sources
                    .iter()
                    .all(|group| group.instant_spacing.is_none()));
                continue;
            }
            let count = (seconds * source) as usize;
            assert_eq!(fixes.occurrences.instants, count);
            assert_eq!(fixes.rate_denominator.duration_ns(), Some(duration));
            let rate = fixes.rate_denominator.per_second(count).unwrap();
            assert!((rate - count as f64 * 1_000_000_000.0 / duration as f64).abs() < 1e-12);
            let imported: Vec<_> = trace
                .events
                .iter()
                .filter(|event| event.name == fix_name)
                .collect();
            let native: Vec<_> = raw.iter().filter(|note| note.label == fix_name).collect();
            assert_eq!(imported.len(), native.len());
            for (event, note) in imported.iter().zip(&native) {
                assert_eq!(event.kind, TimingEventKind::Instant);
                assert_eq!(event.start_ns + start, note.time_ns);
            }
            // Expected gaps come from independently decoded native bytes,
            // already matched one-to-one against the fixture's operation
            // brackets above. Quiet capture edges are not additional gaps.
            assert_eq!(fix_sources.len(), 1);
            let spacing = fix_sources[0].instant_spacing.as_ref().unwrap();
            let minimum = native
                .windows(2)
                .map(|pair| pair[1].time_ns - pair[0].time_ns)
                .min()
                .unwrap();
            let maximum = native
                .windows(2)
                .map(|pair| pair[1].time_ns - pair[0].time_ns)
                .max()
                .unwrap();
            let maximum_pair = native
                .windows(2)
                .find(|pair| pair[1].time_ns - pair[0].time_ns == maximum)
                .unwrap();
            let total_gaps = native.last().unwrap().time_ns - native[0].time_ns;
            assert_eq!(spacing.gap_count, count - 1);
            assert_eq!(spacing.min_ns, minimum);
            assert_eq!(spacing.sum_ns, u128::from(total_gaps));
            assert_eq!(
                spacing.max_gap,
                TimingRange {
                    start_ns: maximum_pair[0].time_ns - start,
                    end_ns: maximum_pair[1].time_ns - start,
                }
            );
            assert_eq!(
                spacing.mean_ns(),
                Some(total_gaps as f64 / (count - 1) as f64)
            );
            assert!(spacing.sum_ns < u128::from(duration));
            let scopes: Vec<_> = trace
                .events
                .iter()
                .filter(|event| event.name == scope_name)
                .collect();
            let beginnings: Vec<_> = raw
                .iter()
                .filter(|note| note.label == scope_name && note.edge == 'B')
                .collect();
            let endings: Vec<_> = raw
                .iter()
                .filter(|note| note.label == scope_name && note.edge == 'E')
                .collect();
            assert_eq!(scopes.len(), count);
            let mut total = 0_u128;
            for ((event, begin), end) in scopes.iter().zip(beginnings).zip(endings) {
                assert_eq!(event.kind, TimingEventKind::Interval);
                assert_eq!(event.start_ns + start, begin.time_ns);
                assert_eq!(event.end_ns + start, end.time_ns);
                total += u128::from(end.time_ns - begin.time_ns);
            }
            let costs = trace.stats(trace.bounds, &scope_name);
            assert_eq!(costs.occurrences.complete_intervals, count);
            assert_eq!(costs.occurrences.full_duration_sum_ns, total);
            assert!(
                (costs.occurrences.mean_duration_ns().unwrap() - total as f64 / count as f64).abs()
                    < 1e-9
            );
        }
        if !overflow {
            assert_eq!(
                trace.events.len(),
                operations.len() * 2,
                "exact imported semantic inventory: one interval and one fix per operation"
            );
            let quiet = trace.stats(
                TimingRange {
                    start_ns: 0,
                    end_ns: 100_000_000,
                },
                "",
            );
            assert_eq!(
                quiet.occurrences.instants + quiet.occurrences.interval_starts,
                0
            );
            assert_eq!(quiet.rate_denominator.per_second(0), Some(0.0));
            assert!(trace.events.iter().all(
                |event| event.start_ns >= 100_000_000 && event.end_ns <= duration - 100_000_000
            ));
        }
        println!(
            "{name}: {duration} ns actual window; {} independent operations; overflow={overflow}",
            operations.len()
        );
    }
    println!("Production timed capture gate passed: sparse 2 s/10 s windows, native bytes/checksums, slow occurrence rates and exact instant gaps, elapsed costs, overflow suppression and killed-owner recovery.");
}
