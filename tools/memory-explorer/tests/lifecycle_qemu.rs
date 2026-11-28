//! Independent admission of same-run QEMU memory and timing evidence. This
//! fixture-specific oracle decodes saved native marker bytes and raw UART text
//! without calling the Python binder, trace converter, or debugger exporter.

use nxrs_memory_explorer::{
    correlate_checkpoints, load_inputs, load_timing, Snapshot, SpanKind, TimeConfidence,
    TimingEventKind,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

mod support;
use support::source_pins;

const CLOCK: &str = "qemu-an521-cmsdk-timer1-20mhz";

fn json(path: &Path) -> Value {
    serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
}

fn text<'a>(value: &'a Value, field: &str) -> &'a str {
    value[field].as_str().unwrap()
}

fn number(value: &Value, field: &str) -> u64 {
    value[field].as_u64().unwrap()
}

fn array<'a>(value: &'a Value, field: &str) -> &'a [Value] {
    value[field].as_array().unwrap()
}

fn sha256(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}

fn validate_sources(build: &Value, pins: &BTreeMap<&str, String>) {
    for source in ["nuttx", "apps"] {
        assert_eq!(text(build, &format!("{source}_revision")), pins[source]);
        assert_eq!(
            text(&build["source_identity"][source], "revision"),
            pins[source]
        );
        assert_eq!(
            build["source_identity"][source]["tracked_sources_clean"],
            true
        );
    }
}

#[test]
fn lifecycle_receipts_require_current_clean_source_pins() {
    let pins = source_pins();
    let build = serde_json::json!({
        "nuttx_revision": pins["nuttx"], "apps_revision": pins["apps"],
        "source_identity": {
            "nuttx": {"revision": pins["nuttx"], "tracked_sources_clean": true},
            "apps": {"revision": pins["apps"], "tracked_sources_clean": true}
        }
    });
    validate_sources(&build, &pins);
    for pointer in [
        "/nuttx_revision",
        "/apps_revision",
        "/source_identity/nuttx/revision",
        "/source_identity/apps/revision",
    ] {
        let mut changed = build.clone();
        *changed.pointer_mut(pointer).unwrap() = serde_json::json!("0".repeat(40));
        assert!(std::panic::catch_unwind(|| validate_sources(&changed, &pins)).is_err());
    }
    for source in ["nuttx", "apps"] {
        let mut changed = build.clone();
        changed["source_identity"][source]["tracked_sources_clean"] = Value::Bool(false);
        assert!(std::panic::catch_unwind(|| validate_sources(&changed, &pins)).is_err());
    }
}

fn fields(line: &str) -> BTreeMap<&str, &str> {
    line.split_whitespace()
        .filter_map(|word| word.split_once('='))
        .collect()
}

fn uart_checkpoints(console: &str, recording_id: &str) -> (BTreeMap<u64, (u64, u64)>, u64, u64) {
    assert!(!console.contains("NXRS_TIMING_WITNESS FAIL"));
    assert_eq!(
        console.matches("NXRS_TIMING_WITNESS PASS rounds=4").count(),
        1
    );
    let lines: Vec<_> = console.lines().collect();
    let boundary = |prefix| {
        let entries: Vec<_> = lines
            .iter()
            .enumerate()
            .filter(|(_, line)| line.starts_with(prefix))
            .collect();
        assert_eq!(entries.len(), 1, "require one {prefix}");
        (entries[0].0, fields(entries[0].1))
    };
    let (start, begin) = boundary("NXRS_NOTE_BEGIN ");
    let (end, finish) = boundary("NXRS_NOTE_END ");
    assert!(start < end);
    for frame in [&begin, &finish] {
        assert_eq!(frame["schema"], "1");
        assert_eq!(frame["recording_id"], recording_id);
        assert_eq!(frame["mode"], "0");
        assert_eq!(frame["notes_lost"], "0");
    }
    for (key, expected) in [
        ("clock", CLOCK),
        ("clock_bits", "32"),
        ("frequency_hz", "20000000"),
        ("clock_epoch", "fixture-invocation"),
        ("cpus", "1"),
        ("capture_mode", "stopped-checkpoints"),
        ("checkpoints", "4"),
    ] {
        assert_eq!(begin[key], expected);
    }
    assert_eq!(finish["complete"], "1");
    let mut checkpoints = BTreeMap::new();
    let mut first = None;
    let mut last = 0;
    for line in &lines[start + 1..end] {
        let (_, body) = line.split_once("[0]").expect("single-core native note");
        let (stamp, event) = body.trim().split_once(": ").unwrap();
        let (seconds, nanos) = stamp.split_once('.').unwrap();
        assert_eq!(nanos.len(), 9);
        let timestamp = seconds
            .parse::<u64>()
            .unwrap()
            .checked_mul(1_000_000_000)
            .unwrap()
            .checked_add(nanos.parse::<u64>().unwrap())
            .unwrap();
        assert!(timestamp >= last, "raw clock ran backward");
        first.get_or_insert(timestamp);
        last = timestamp;
        if event.contains("memory/checkpoint/") {
            let payload = event
                .strip_prefix("tracing_mark_write: I|")
                .expect("checkpoint must be native instant");
            let (task, name) = payload.split_once('|').unwrap();
            let suffix = name.strip_prefix("memory/checkpoint/").unwrap();
            let id = suffix.parse::<u64>().unwrap();
            assert_eq!(suffix, id.to_string());
            assert!((1..=4).contains(&id));
            assert!(
                checkpoints
                    .insert(id, (timestamp, task.parse::<u64>().unwrap()))
                    .is_none(),
                "duplicate native checkpoint"
            );
        }
    }
    assert_eq!(checkpoints.len(), 4);
    let first = first.expect("native notes");
    assert!(first < last && last < 5_000_000_000);
    (checkpoints, first, last)
}

fn native_marker_bytes(marker: &Value, id: u64, expected_time: u64, expected_task: u64) {
    let hex = text(marker, "raw_note_hex");
    assert_eq!(hex.len() % 2, 0);
    let bytes: Vec<_> = (0..hex.len())
        .step_by(2)
        .map(|offset| u8::from_str_radix(&hex[offset..offset + 2], 16).unwrap())
        .collect();
    let name = format!("memory/checkpoint/{id}");
    // Pinned ARM EABI: note_common_s is 16 bytes, with pid:u32 at 4 and
    // clock_t:u64 at 8. note_event_s has payload at 24 and padded sizeof 32.
    // The native macro includes that trailing structure padding in nc_length.
    // This oracle deliberately does not consume the debugger's decoded fields
    // to choose offsets; its supported ABI is fixed by this witness and pin.
    assert_eq!(bytes.len(), 32 + name.len());
    assert_eq!(usize::from(bytes[0]), bytes.len());
    assert_eq!(bytes[1], 33, "pinned NOTE_DUMP_MARK enum value");
    assert_eq!(bytes[3], 0, "single CPU");
    assert_eq!(
        u64::from(u32::from_le_bytes(bytes[4..8].try_into().unwrap())),
        expected_task
    );
    let ticks = u64::from_le_bytes(bytes[8..16].try_into().unwrap());
    assert_eq!(ticks.checked_mul(50).unwrap(), expected_time);
    assert_eq!(
        u32::from_le_bytes(bytes[20..24].try_into().unwrap()),
        0,
        "NOTE_TAG_ALWAYS"
    );
    assert_eq!(&bytes[24..24 + name.len()], name.as_bytes());
    assert_eq!(number(marker, "clock_ticks"), ticks);
    assert_eq!(number(marker, "source_time_ns"), expected_time);
    assert_eq!(number(marker, "task_id"), expected_task);
    assert_eq!(number(marker, "cpu"), 0);
    assert_eq!(text(marker, "name"), name);
    assert_eq!(number(marker, "ring_size"), 32768);
    assert!(number(marker, "ring_head") < 32768 && number(marker, "ring_tail") < 32768);
}

fn verify_memory(snapshot: &Snapshot, document: &Value, raw: &Value) {
    let raw_regions: Vec<_> = array(raw, "heaps")
        .iter()
        .flat_map(|heap| array(heap, "regions"))
        .collect();
    assert_eq!(
        snapshot
            .spans
            .iter()
            .filter(|span| span.kind == SpanKind::HeapArena)
            .count(),
        raw_regions.len()
    );
    let mut chunk_count = 0;
    for region in raw_regions {
        let matching: Vec<_> = snapshot
            .spans
            .iter()
            .filter(|span| {
                span.kind == SpanKind::HeapArena
                    && span.start == number(region, "start")
                    && span.size == number(region, "size")
            })
            .collect();
        assert_eq!(
            matching.len(),
            1,
            "preserve each raw arena's exact geometry"
        );
        let mut expected: Vec<_> = array(region, "chunks")
            .iter()
            .map(|chunk| {
                let kind = match text(chunk, "kind") {
                    "allocation" => SpanKind::Allocation,
                    "free" => SpanKind::Free,
                    "reserved" => SpanKind::Reserved,
                    other => panic!("unexpected raw kind {other}"),
                };
                (number(chunk, "start"), number(chunk, "size"), kind)
            })
            .collect();
        chunk_count += expected.len();
        let mut actual: Vec<_> = snapshot
            .spans
            .iter()
            .filter(|span| span.parent.as_deref() == Some(matching[0].id.as_str()))
            .map(|span| (span.start, span.size, span.kind))
            .collect();
        actual.sort();
        expected.sort();
        assert_eq!(
            actual, expected,
            "every raw allocator boundary/state must match"
        );
    }
    assert_eq!(
        snapshot
            .spans
            .iter()
            .filter(|span| matches!(
                span.kind,
                SpanKind::Allocation | SpanKind::Free | SpanKind::Reserved
            ))
            .count(),
        chunk_count
    );
    let raw_tasks: BTreeMap<_, _> = array(raw, "tasks")
        .iter()
        .map(|task| (number(task, "tcb"), task))
        .collect();
    let capture_tasks = array(&document["capture"], "tasks");
    assert_eq!(capture_tasks.len(), raw_tasks.len());
    assert_eq!(
        capture_tasks
            .iter()
            .map(|task| number(task, "tcb"))
            .collect::<BTreeSet<_>>(),
        raw_tasks.keys().copied().collect(),
        "task inventory must be a bijection"
    );
    let mut represented = 0;
    for task in capture_tasks {
        let direct = raw_tasks[&number(task, "tcb")];
        for field in ["pid", "stack_alloc_ptr", "stack_base_ptr", "adj_stack_size"] {
            assert_eq!(number(task, field), number(direct, field));
        }
        if let Some(id) = task["span"].as_str() {
            let span = snapshot.spans.iter().find(|span| span.id == id).unwrap();
            assert_eq!(span.kind, SpanKind::Stack);
            assert_eq!(span.start, number(direct, "stack_base_ptr"));
            assert_eq!(span.size, number(direct, "adj_stack_size"));
            represented += 1;
        }
    }
    assert!(
        represented > 0,
        "actual task reservations must be represented"
    );
    assert_eq!(
        snapshot
            .spans
            .iter()
            .filter(|span| span.kind == SpanKind::Stack)
            .count(),
        represented
    );
}

fn contains_payload(snapshot: &Snapshot, pointer: u64, bytes: u64, kind: SpanKind) -> bool {
    snapshot
        .spans
        .iter()
        .filter(|span| {
            span.kind == kind
                && span.start < pointer
                && pointer
                    .checked_add(bytes)
                    .is_some_and(|end| end <= span.end())
        })
        .count()
        == 1
}

#[test]
#[ignore = "requires qualify_lifecycle_qemu.py evidence and exact ELF through NXRS_MEMORY_LIFECYCLE_* env"]
fn links_actual_stopped_heap_states_to_independent_native_and_uart_markers() {
    let evidence = PathBuf::from(
        std::env::var_os("NXRS_MEMORY_LIFECYCLE_EVIDENCE")
            .expect("set NXRS_MEMORY_LIFECYCLE_EVIDENCE to the capture directory"),
    );
    let elf = PathBuf::from(
        std::env::var_os("NXRS_MEMORY_LIFECYCLE_ELF")
            .expect("set NXRS_MEMORY_LIFECYCLE_ELF to the exact native witness ELF"),
    );
    let build_dir = elf.parent().unwrap();
    let build = json(&build_dir.join("build.json"));
    let report = json(&evidence.join("report.json"));
    let run = json(&evidence.join("run.json"));
    let recording_id = text(&run, "recording_id");
    assert_eq!(recording_id.len(), 32);
    assert!(recording_id.bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert_eq!(
        text(&run, "command"),
        format!("memory_timing --checkpoints {recording_id}")
    );
    let command = array(&run, "qemu_command");
    let kernel = command.iter().position(|arg| arg == "-kernel").unwrap();
    assert_eq!(command[kernel + 1], text(&run, "image"));
    assert_eq!(text(&report, "recording_id"), recording_id);
    assert_eq!(number(&report, "schema_version"), 1);
    assert_eq!(
        text(&report, "evidence"),
        "real_nuttx_qemu_linked_lifecycle"
    );
    assert_eq!(text(&report, "machine"), "mps2-an521");
    assert_eq!(text(&report, "clock"), CLOCK);
    let console = fs::read_to_string(evidence.join("console.log")).unwrap();
    let (markers, first, last) = uart_checkpoints(&console, recording_id);

    validate_sources(&build, &source_pins());
    assert_eq!(build["qemu_cmsdk_clock_adapter"], true);
    assert_eq!(number(&build["clock_contract"], "frequency_hz"), 20_000_000);
    assert_eq!(number(&build["clock_contract"], "bits"), 32);
    assert_eq!(
        number(&build["clock_contract"], "wrap_period_ns"),
        214_748_364_800
    );
    assert_eq!(build["clock_contract"]["irq_enabled"], false);
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/nuttx-std/memory-timing");
    let fixture_hashes = build["fixture_sha256"].as_object().unwrap();
    assert_eq!(fixture_hashes.len(), 5);
    for name in [
        "Kconfig",
        "Make.defs",
        "Makefile",
        "timing_main.c",
        "qemu_timer_clock.c",
    ] {
        assert_eq!(
            fixture_hashes[name],
            sha256(&fixture.join(name)),
            "fixture changed since build"
        );
    }
    let config_sha = sha256(&build_dir.join("resolved.config"));
    assert_eq!(sha256(&evidence.join("resolved.config")), config_sha);
    for receipt in [&build, &run, &report] {
        assert_eq!(text(receipt, "elf_sha256"), sha256(&elf));
        assert_eq!(text(receipt, "config_sha256"), config_sha);
    }
    let config = fs::read_to_string(evidence.join("resolved.config")).unwrap();
    assert!(!config.lines().any(|line| line == "CONFIG_SMP=y"));
    for line in [
        "CONFIG_ARCH_BOARD_MPS2_AN521=y",
        "CONFIG_BUILD_FLAT=y",
        "CONFIG_MM_DEFAULT_MANAGER=y",
        "CONFIG_DRIVERS_NOTE_MAX=1",
        "CONFIG_DRIVERS_NOTERAM_BUFSIZE=32768",
        "CONFIG_EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK=y",
    ] {
        assert!(
            config.lines().any(|actual| actual == line),
            "missing configuration {line}"
        );
    }
    let elf_bytes = fs::read(&elf).unwrap();
    assert_eq!(
        &elf_bytes[..6],
        b"\x7fELF\x01\x01",
        "fixed little-endian ELF32 note ABI"
    );

    let points = array(&report, "snapshots");
    assert_eq!(points.len(), 4);
    let paths: Vec<_> = points
        .iter()
        .map(|point| evidence.join(text(point, "capture_file")))
        .collect();
    let layout = Path::new(env!("CARGO_MANIFEST_DIR")).join("layouts/mps2-an521-qemu.json");
    let model = load_inputs(&elf, None, &paths, Some(&layout)).unwrap();
    for symbol in [
        "memory_timing_main",
        "timing_checkpoint_stop",
        "g_memory_recording_id",
        "__wrap_up_perf_gettime",
    ] {
        assert!(
            model.symbols.iter().any(|entry| entry.name == symbol),
            "missing symbol {symbol}"
        );
    }
    let trace = load_timing(&evidence.join("timing.json")).unwrap();
    assert_eq!(trace.metadata.origin_ns, Some(first));
    assert_eq!(trace.bounds.start_ns, 0);
    assert_eq!(trace.bounds.end_ns, last - first);
    assert_eq!(trace.metadata.lost_records, Some(0));
    assert_eq!(trace.metadata.loss_detected, Some(false));
    assert_eq!(trace.metadata.source.as_deref(), Some("qemu-mps2-an521"));
    let correlation = correlate_checkpoints(&model, &trace).unwrap().unwrap();
    assert_eq!(correlation.recording_id, recording_id);
    assert_eq!(correlation.clock, CLOCK);
    assert_eq!(correlation.links.len(), 4);
    assert!(trace
        .events
        .iter()
        .any(|event| event.category == "sched,cpu" && event.kind == TimingEventKind::Interval));
    assert!(trace
        .events
        .iter()
        .any(|event| event.category == "irq" && event.kind == TimingEventKind::Interval));
    assert_eq!(
        trace
            .events
            .iter()
            .filter(|event| event.name == "timing/busy" && event.kind == TimingEventKind::Interval)
            .count(),
        4
    );

    let mut pointers = Vec::new();
    for (index, point) in points.iter().enumerate() {
        let id = index as u64 + 1;
        let (timestamp, task) = markers[&id];
        assert_eq!(number(point, "checkpoint_id"), id);
        assert_eq!(number(point, "time_ns"), timestamp);
        assert_eq!(point["metadata_unchanged"], true);
        let raw = json(&evidence.join(text(point, "raw_file")));
        assert_eq!(text(&raw, "elf_sha256"), model.build.elf_sha256);
        let checkpoint = &raw["checkpoint"];
        assert_eq!(text(checkpoint, "recording_id"), recording_id);
        assert_eq!(number(checkpoint, "checkpoint_id"), id);
        assert_eq!(number(checkpoint, "primask"), 1);
        assert!(number(checkpoint, "basepri") > 0);
        assert_eq!(number(checkpoint, "task_id"), task);
        assert_eq!(checkpoint["timer_unchanged_while_stopped"], true);
        let native_marker = &checkpoint["marker"];
        native_marker_bytes(native_marker, id, timestamp, task);
        assert_eq!(point["marker"], *native_marker);
        let stopped_ticks = number(checkpoint, "stopped_timer_ticks");
        assert!(
            number(native_marker, "clock_ticks") <= stopped_ticks && stopped_ticks < 100_000_000
        );
        let capture = json(&paths[index]);
        let unbound = json(&evidence.join(text(point, "unbound_file")));
        assert_eq!(
            capture["snapshots"][0]["spans"],
            unbound["snapshots"][0]["spans"]
        );
        assert_eq!(unbound["snapshots"][0]["time_ns"], timestamp);
        assert!(unbound["snapshots"][0].get("correlation").is_none());
        let snapshot = model.snapshot_at(timestamp).unwrap();
        verify_memory(snapshot, &capture, &raw);
        let link = correlation.link_for_snapshot(timestamp).unwrap();
        assert_eq!(link.checkpoint_id, id);
        assert_eq!(link.trace_time_ns, timestamp - first);
        let event = trace
            .events
            .iter()
            .find(|event| event.id == link.event_id)
            .unwrap();
        assert_eq!(event.name, format!("memory/checkpoint/{id}"));
        assert_eq!(number(&event.args, "task_id"), task);
        pointers.push(
            array(checkpoint, "block_pointers")
                .iter()
                .map(|value| value.as_u64().unwrap())
                .collect::<Vec<_>>(),
        );
        println!(
            "Checkpoint {id}: source {timestamp} ns, trace {} ns, {} memory spans",
            link.trace_time_ns,
            snapshot.spans.len()
        );
    }
    assert_eq!(pointers[0], [0, 0, 0]);
    assert_eq!(pointers[3], [0, 0, 0]);
    assert_eq!(pointers[1].len(), 3);
    assert_eq!(pointers[2], [pointers[1][0], 0, pointers[1][2]]);
    let mut allocated_chunks = Vec::new();
    for (index, size) in [16384, 32768, 16384].into_iter().enumerate() {
        let pointer = pointers[1][index];
        assert!(contains_payload(
            &model.snapshots[1],
            pointer,
            size,
            SpanKind::Allocation
        ));
        let chunk = model.snapshots[1]
            .spans
            .iter()
            .find(|span| {
                span.kind == SpanKind::Allocation
                    && span.start < pointer
                    && pointer + size <= span.end()
            })
            .unwrap();
        allocated_chunks.push((chunk.start, chunk.end()));
        assert!(contains_payload(
            &model.snapshots[3],
            pointer,
            size,
            SpanKind::Free
        ));
        assert!(contains_payload(
            &model.snapshots[2],
            pointer,
            size,
            if index == 1 {
                SpanKind::Free
            } else {
                SpanKind::Allocation
            }
        ));
    }
    assert_eq!(
        allocated_chunks[0].1, allocated_chunks[1].0,
        "first two allocation chunks must be adjacent"
    );
    assert_eq!(
        allocated_chunks[1].1, allocated_chunks[2].0,
        "last two allocation chunks must be adjacent"
    );
    for index in [0, 2] {
        assert!(
            model.snapshots[2]
                .spans
                .iter()
                .any(|span| span.kind == SpanKind::Allocation
                    && (span.start, span.end()) == allocated_chunks[index]),
            "neighbor allocation boundaries must survive the middle release"
        );
    }
    assert!(
        model.snapshots[2]
            .spans
            .iter()
            .any(|span| span.kind == SpanKind::Free
                && (span.start, span.end()) == allocated_chunks[1]),
        "middle release must create the exact interior hole"
    );
    for pair in correlation.links.windows(2) {
        let time = pair[0].trace_time_ns + 1;
        assert!(time < pair[1].trace_time_ns);
        assert!(correlation.link_at_trace_time(time).is_none());
        let source = correlation.source_time_ns(time).unwrap();
        for region in &model.regions {
            assert_eq!(
                model
                    .view(&region.id, region.range().unwrap(), source)
                    .runtime,
                TimeConfidence::Unknown
            );
            assert_eq!(model.free_bytes(&region.id, source), 0);
        }
    }
    println!("Same-run lifecycle gate passed: four exact native/UART/checkpoint links; three allocated payloads, one real freed hole, and all three released payloads verified against raw allocator boundaries.");
}
