//! An opt-in comparison of actual UART notes, build receipts and imported timing.
//! The small oracle understands this single-core qualification fixture only; it
//! does not call the converter or manufacture target observations.

use nxrs_memory_explorer::{load_inputs, load_timing, TimingEventKind};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

fn json(path: &Path) -> Value {
    serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
}

fn text<'a>(value: &'a Value, field: &str) -> &'a str {
    value[field].as_str().unwrap()
}

fn number(value: &Value, field: &str) -> u64 {
    value[field].as_u64().unwrap()
}

fn sha256(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}

fn fields(payload: &str) -> BTreeMap<&str, &str> {
    payload
        .split_whitespace()
        .filter_map(|word| word.split_once('='))
        .collect()
}

#[derive(Debug)]
struct Note<'a> {
    time: u64,
    kind: &'a str,
    payload: &'a str,
}

fn notes(console: &str) -> (Vec<Note<'_>>, Value, Value) {
    assert!(!console.contains("NXRS_TIMING_WITNESS FAIL"));
    assert_eq!(
        console.matches("NXRS_TIMING_WITNESS PASS rounds=4").count(),
        1
    );
    let lines: Vec<_> = console.lines().collect();
    let frame = |tag: &str| {
        let matching: Vec<_> = lines
            .iter()
            .enumerate()
            .filter(|(_, line)| line.starts_with(tag))
            .collect();
        assert_eq!(matching.len(), 1, "require exactly one {tag}");
        let (index, line) = matching[0];
        (index, serde_json::to_value(fields(line)).unwrap())
    };
    let (begin, begin_fields) = frame("NXRS_NOTE_BEGIN ");
    let (end, end_fields) = frame("NXRS_NOTE_END ");
    assert!(end > begin);
    let mut result: Vec<Note<'_>> = Vec::new();
    for line in &lines[begin + 1..end] {
        let (task, record) = line.split_once("[0]").expect("single-core target record");
        task.trim()
            .rsplit_once('-')
            .unwrap()
            .1
            .parse::<u64>()
            .unwrap();
        let (stamp, event) = record.trim().split_once(": ").unwrap();
        let (seconds, nanos) = stamp.split_once('.').unwrap();
        assert_eq!(nanos.len(), 9);
        let time = seconds.parse::<u64>().unwrap() * 1_000_000_000 + nanos.parse::<u64>().unwrap();
        let (kind, payload) = event.split_once(": ").unwrap();
        if let Some(previous) = result.last() {
            assert!(time >= previous.time, "raw target clock ran backward");
        }
        result.push(Note {
            time,
            kind,
            payload,
        });
    }
    (result, begin_fields, end_fields)
}

#[test]
#[ignore = "requires real native QEMU timing evidence and exact ELF through NXRS_MEMORY_TIMING_* env"]
fn imports_real_timing_and_matches_independent_uart_endpoints() {
    let evidence = PathBuf::from(
        std::env::var_os("NXRS_MEMORY_TIMING_EVIDENCE")
            .expect("set NXRS_MEMORY_TIMING_EVIDENCE to qualify_timing_qemu.py's output directory"),
    );
    let elf = PathBuf::from(
        std::env::var_os("NXRS_MEMORY_TIMING_ELF")
            .expect("set NXRS_MEMORY_TIMING_ELF to the built native witness ELF"),
    );
    let build_dir = elf.parent().unwrap();
    let build = json(&build_dir.join("build.json"));
    let report = json(&evidence.join("report.json"));
    let console = fs::read_to_string(evidence.join("console.log")).unwrap();
    let (raw, begin, end) = notes(&console);
    assert!(
        raw.len() > 20,
        "require actual scheduler/IRQ/application work"
    );
    let first = raw.first().unwrap().time;
    let last = raw.last().unwrap().time;
    assert!(last > first && last < 5_000_000_000);

    assert_eq!(number(&report, "schema_version"), 1);
    assert_eq!(text(&report, "evidence"), "real_nuttx_qemu_native_timing");
    assert_eq!(text(&report, "machine"), "mps2-an521");
    assert_eq!(report["monotonic_records"], true);
    assert_eq!(report["complete"], true);
    assert_eq!(number(&report, "lost_records"), 0);
    assert_eq!(number(&report, "first_record_ns"), first);
    assert_eq!(number(&report, "last_record_ns"), last);
    assert_eq!(report["framing"]["NXRS_NOTE_BEGIN"], begin);
    assert_eq!(report["framing"]["NXRS_NOTE_END"], end);
    for frame in [&begin, &end] {
        assert_eq!(text(frame, "mode"), "0");
        assert_eq!(text(frame, "notes_lost"), "0");
    }
    assert_eq!(text(&begin, "cpus"), "1");
    assert_eq!(text(&begin, "clock"), "qemu-an521-cmsdk-timer1-20mhz");
    assert_eq!(text(&begin, "clock_bits"), "32");
    assert_eq!(text(&begin, "clock_epoch"), "fixture-invocation");
    assert_eq!(text(&begin, "frequency_hz"), "20000000");
    assert_eq!(text(&end, "complete"), "1");

    assert_eq!(
        text(&build, "nuttx_revision"),
        "2b5c0de5e933281c209f954a63b99cf10c132346"
    );
    assert_eq!(
        text(&build, "apps_revision"),
        "3c772ea35b9245972e18999828f7b533e6fc57cc"
    );
    for source in ["nuttx", "apps"] {
        assert_eq!(
            build["source_identity"][source]["tracked_sources_clean"],
            true
        );
    }
    assert_eq!(build["qemu_cmsdk_clock_adapter"], true);
    assert_eq!(number(&build["clock_contract"], "frequency_hz"), 20_000_000);
    assert_eq!(number(&build["clock_contract"], "bits"), 32);
    assert_eq!(
        number(&build["clock_contract"], "wrap_period_ns"),
        214_748_364_800
    );
    assert_eq!(build["clock_contract"]["irq_enabled"], false);
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/nuttx-std/memory-timing");
    let hashes = build["fixture_sha256"].as_object().unwrap();
    assert_eq!(hashes.len(), 5);
    for name in [
        "Kconfig",
        "Make.defs",
        "Makefile",
        "timing_main.c",
        "qemu_timer_clock.c",
    ] {
        assert_eq!(
            hashes[name],
            sha256(&fixture.join(name)),
            "fixture changed since build"
        );
    }
    let config_sha = sha256(&build_dir.join("resolved.config"));
    assert_eq!(sha256(&evidence.join("resolved.config")), config_sha);
    assert_eq!(text(&build, "config_sha256"), config_sha);
    assert_eq!(text(&report, "config_sha256"), config_sha);
    let model = load_inputs(&elf, None, &[], None).unwrap();
    assert_eq!(model.build.elf_sha256, sha256(&elf));
    assert_eq!(text(&build, "elf_sha256"), model.build.elf_sha256);
    assert_eq!(text(&report, "elf_sha256"), model.build.elf_sha256);
    assert!(!model.static_spans.is_empty());
    for symbol in [
        "memory_timing_main",
        "__wrap_up_perf_gettime",
        "__wrap_up_perf_convert",
    ] {
        assert!(
            model.symbols.iter().any(|entry| entry.name == symbol),
            "missing target symbol {symbol}"
        );
    }

    let trace = load_timing(&evidence.join("timing.json")).unwrap();
    assert_eq!(
        trace.metadata.elf_sha256.as_deref(),
        Some(model.build.elf_sha256.as_str())
    );
    assert_eq!(trace.metadata.clock.as_deref(), Some(text(&begin, "clock")));
    assert_eq!(trace.metadata.source.as_deref(), Some("qemu-mps2-an521"));
    assert_eq!(trace.metadata.origin_ns, Some(first));
    assert_eq!(trace.metadata.lost_records, Some(0));
    assert_eq!(trace.metadata.loss_detected, Some(false));
    assert_eq!(trace.bounds.start_ns, 0);
    assert_eq!(trace.bounds.end_ns, last - first);

    let mut switches = Vec::new();
    let mut irq_stack = Vec::new();
    let mut expected_irqs = Vec::new();
    let mut app_stack: BTreeMap<u64, Vec<(&str, u64)>> = BTreeMap::new();
    let mut expected_apps = Vec::new();
    let mut expected_counters = Vec::new();
    let mut expected_notes = Vec::new();
    for note in &raw {
        let values = fields(note.payload);
        match note.kind {
            "sched_switch" => switches.push((
                note.time,
                values["prev_pid"].parse::<u64>().unwrap(),
                values["next_pid"].parse::<u64>().unwrap(),
            )),
            "irq_handler_entry" => {
                irq_stack.push((note.time, values["irq"].parse::<u64>().unwrap()))
            }
            "irq_handler_exit" => {
                let (start, irq) = irq_stack.pop().expect("IRQ exit needs an observed entry");
                assert_eq!(irq, values["irq"].parse::<u64>().unwrap());
                expected_irqs.push((start, note.time, irq));
            }
            "tracing_mark_write" => {
                let parts: Vec<_> = note.payload.split('|').collect();
                let task = parts[1].parse::<u64>().unwrap();
                match parts[0] {
                    "B" => app_stack
                        .entry(task)
                        .or_default()
                        .push((parts[2], note.time)),
                    "E" => {
                        let (name, start) = app_stack.entry(task).or_default().pop().unwrap();
                        assert_eq!(parts[2], name);
                        expected_apps.push((start, note.time, task, name.to_owned()));
                    }
                    "C" => expected_counters.push((
                        note.time,
                        parts[2].to_owned(),
                        parts[3].to_owned(),
                    )),
                    other => panic!("unexpected witness marker {other}"),
                }
            }
            _ => expected_notes.push((note.time, note.kind.to_owned(), note.payload.to_owned())),
        }
    }
    assert!(irq_stack.is_empty());
    assert!(app_stack.values().all(Vec::is_empty));
    for kind in [
        "sched_switch",
        "irq_handler_entry",
        "irq_handler_exit",
        "tracing_mark_write",
    ] {
        assert_eq!(
            number(&report["observed_records"], kind) as usize,
            raw.iter().filter(|note| note.kind == kind).count()
        );
    }
    assert!(switches.len() > 3 && !expected_irqs.is_empty());
    let expected_residencies: Vec<_> = switches
        .windows(2)
        .map(|pair| {
            assert_eq!(pair[0].2, pair[1].1, "scheduler task continuity");
            (pair[0].0, pair[1].0, pair[0].2)
        })
        .collect();
    let actual_intervals = |category: &str, id: &str| {
        let mut intervals: Vec<_> = trace
            .events
            .iter()
            .filter(|event| event.category == category)
            .map(|event| {
                assert_eq!(event.kind, TimingEventKind::Interval);
                (
                    event.start_ns + first,
                    event.end_ns + first,
                    number(&event.args, id),
                )
            })
            .collect();
        intervals.sort();
        intervals
    };
    expected_irqs.sort();
    assert_eq!(actual_intervals("irq", "irq"), expected_irqs);
    assert_eq!(
        actual_intervals("sched,cpu", "task_id"),
        expected_residencies
    );
    assert_eq!(
        actual_intervals("sched,task", "task_id"),
        expected_residencies
    );
    let mut actual_apps: Vec<_> = trace
        .events
        .iter()
        .filter(|event| event.category == "app")
        .map(|event| {
            assert_eq!(event.kind, TimingEventKind::Interval);
            (
                event.start_ns + first,
                event.end_ns + first,
                number(&event.args, "task_id"),
                event.name.clone(),
            )
        })
        .collect();
    actual_apps.sort();
    expected_apps.sort();
    assert_eq!(actual_apps, expected_apps);
    for (name, count) in [
        ("timing/capture", 1),
        ("timing/dispatch", 4),
        ("timing/worker", 4),
        ("timing/busy", 4),
        ("timing/sleep", 4),
    ] {
        assert_eq!(
            expected_apps.iter().filter(|entry| entry.3 == name).count(),
            count
        );
    }
    let mut actual_counters: Vec<_> = trace
        .counters
        .iter()
        .map(|sample| {
            assert_eq!(sample.series, "value");
            (
                sample.time_ns + first,
                sample.name.clone(),
                sample.exact_value.clone(),
            )
        })
        .collect();
    actual_counters.sort();
    expected_counters.sort();
    assert_eq!(actual_counters, expected_counters);
    assert_eq!(
        expected_counters
            .iter()
            .map(|entry| entry.2.as_str())
            .collect::<Vec<_>>(),
        ["1", "2", "3", "4"]
    );
    let mut actual_notes: Vec<_> = trace
        .events
        .iter()
        .filter(|event| event.category == "note")
        .map(|event| {
            assert_eq!(event.kind, TimingEventKind::Instant);
            (
                event.start_ns + first,
                event.name.clone(),
                text(&event.args, "payload").to_owned(),
            )
        })
        .collect();
    actual_notes.sort();
    expected_notes.sort();
    assert_eq!(actual_notes, expected_notes);

    let censored: Vec<_> = trace
        .events
        .iter()
        .filter(|event| event.category == "unknown,capture")
        .collect();
    assert_eq!(
        censored.len(),
        2,
        "single-core capture has two unpaired scheduler boundaries"
    );
    let mut actual_boundaries: Vec<_> = censored
        .iter()
        .map(|event| {
            assert_eq!(event.kind, TimingEventKind::Instant);
            assert_eq!(
                event.start_ns, event.end_ns,
                "unknown duration stays unmeasured"
            );
            assert_eq!(event.args["duration_unknown"], true);
            (event.start_ns + first, number(&event.args, "task_id"))
        })
        .collect();
    actual_boundaries.sort();
    assert_eq!(
        actual_boundaries,
        [
            (switches[0].0, switches[0].1),
            (switches.last().unwrap().0, switches.last().unwrap().2)
        ]
    );
    assert_eq!(trace.metadata.incomplete_intervals, 2);
    assert_eq!(trace.metadata.complete, Some(false));
    let expected_intervals =
        expected_apps.len() + expected_irqs.len() + 2 * expected_residencies.len();
    assert_eq!(
        trace.events.len(),
        expected_intervals + expected_notes.len() + censored.len()
    );
    assert!(
        trace
            .warnings
            .iter()
            .any(|warning| warning.contains("clock_bits=32")
                && warning.contains("fixture-invocation"))
    );
    println!("Real native QEMU timing import passed: {expected_intervals} measured intervals, {} counters, two unmeasured scheduler boundaries; target clock {first}..{last} ns.", trace.counters.len());
}
