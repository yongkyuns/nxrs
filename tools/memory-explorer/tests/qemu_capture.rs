//! Import real QEMU observations and compare them with the independent raw-byte
//! debugger walk. This test is opt-in: ordinary host fixtures are not target proof.

use nxrs_memory_explorer::{load_inputs, SpanKind, TimeConfidence};
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};

fn read_json(path: &Path) -> Value {
    serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
}

fn number(value: &Value, field: &str) -> u64 {
    value[field]
        .as_u64()
        .unwrap_or_else(|| panic!("missing unsigned integer {field}: {value}"))
}

fn text<'a>(value: &'a Value, field: &str) -> &'a str {
    value[field]
        .as_str()
        .unwrap_or_else(|| panic!("missing string {field}: {value}"))
}

fn array<'a>(value: &'a Value, field: &str) -> &'a [Value] {
    value[field]
        .as_array()
        .unwrap_or_else(|| panic!("missing array {field}: {value}"))
}

#[test]
#[ignore = "requires qualify_qemu.py evidence and the exact target ELF through NXRS_MEMORY_QEMU_* env"]
fn imports_real_qemu_captures_and_matches_independent_target_bytes() {
    let evidence = PathBuf::from(
        std::env::var_os("NXRS_MEMORY_QEMU_EVIDENCE")
            .expect("set NXRS_MEMORY_QEMU_EVIDENCE to qualify_qemu.py's output directory"),
    );
    let elf = PathBuf::from(
        std::env::var_os("NXRS_MEMORY_QEMU_ELF")
            .expect("set NXRS_MEMORY_QEMU_ELF to the exact QEMU firmware ELF"),
    );
    let report = read_json(&evidence.join("report.json"));
    assert_eq!(number(&report, "schema_version"), 1);
    assert_eq!(text(&report, "evidence"), "real_nuttx_qemu");
    assert_eq!(text(&report, "machine"), "mps2-an521");
    assert_eq!(report["workload_passed"], true);
    let observations = array(&report, "snapshots");
    assert_eq!(observations.len(), 3, "require all lifecycle observations");
    let paths: Vec<_> = observations
        .iter()
        .map(|point| evidence.join(text(point, "capture_file")))
        .collect();
    let model = load_inputs(&elf, None, &paths, None).unwrap();
    assert_eq!(model.build.elf_sha256, text(&report, "elf_sha256"));
    assert_eq!(model.snapshots.len(), observations.len());
    assert!(!model.static_spans.is_empty());
    assert!(model
        .symbols
        .iter()
        .any(|symbol| symbol.name == "std_demo_main"));

    let mut total_stacks = 0;
    let mut total_sources = 0;
    for point in observations {
        assert_eq!(point["metadata_unchanged"], true);
        let raw = read_json(&evidence.join(text(point, "raw_file")));
        assert_eq!(text(&raw, "elf_sha256"), model.build.elf_sha256);
        let time = number(point, "time_ns");
        let capture = model.snapshot_at(time).expect("exact captured coordinate");
        assert_eq!(capture.label, text(point, "label"));
        let mut raw_arena_count = 0;
        let mut raw_chunk_count = 0;
        for heap in array(&raw, "heaps") {
            for region in array(heap, "regions") {
                raw_arena_count += 1;
                let start = number(region, "start");
                let size = number(region, "size");
                let arena = capture
                    .spans
                    .iter()
                    .find(|span| {
                        span.kind == SpanKind::HeapArena && span.start == start && span.size == size
                    })
                    .expect("raw target arena must retain its exact address and extent");
                let mut expected = Vec::new();
                let mut allocated = 0;
                let mut free = 0;
                let mut largest = 0;
                let mut reserved = 0;
                for chunk in array(region, "chunks") {
                    raw_chunk_count += 1;
                    let size = number(chunk, "size");
                    let kind = match text(chunk, "kind") {
                        "allocation" => {
                            allocated += size;
                            SpanKind::Allocation
                        }
                        "free" => {
                            free += size;
                            largest = largest.max(size);
                            SpanKind::Free
                        }
                        "reserved" => {
                            reserved += size;
                            SpanKind::Reserved
                        }
                        other => panic!("unexpected independent raw chunk state: {other}"),
                    };
                    expected.push((number(chunk, "start"), size, kind));
                }
                let mut actual: Vec<_> = capture
                    .spans
                    .iter()
                    .filter(|span| span.parent.as_deref() == Some(arena.id.as_str()))
                    .map(|span| (span.start, span.size, span.kind))
                    .collect();
                expected.sort();
                actual.sort();
                assert_eq!(
                    actual, expected,
                    "raw target chunk boundaries/states differ"
                );
                assert_eq!(allocated + free + reserved, arena.size);
                let stats = model
                    .heap_statistics(&arena.region, time)
                    .into_iter()
                    .find(|stats| stats.arena == arena.id)
                    .unwrap();
                assert_eq!(stats.arena_bytes, size);
                assert_eq!(stats.allocated_chunk_bytes, allocated);
                assert_eq!(stats.free_chunk_bytes, free);
                assert_eq!(stats.largest_free_chunk, largest);
                assert_eq!(stats.reserved_bytes, reserved);
                assert_eq!(stats.unknown_bytes, 0);
                println!(
                    "{}: arena {start:#x}+{size:#x}, {} chunks, allocated={allocated}, free={free}, largest={largest}, guards={reserved}",
                    capture.label,
                    actual.len()
                );
            }
        }
        assert_eq!(
            capture
                .spans
                .iter()
                .filter(|span| span.kind == SpanKind::HeapArena)
                .count(),
            raw_arena_count
        );
        assert_eq!(
            capture
                .spans
                .iter()
                .filter(|span| matches!(
                    span.kind,
                    SpanKind::Allocation | SpanKind::Free | SpanKind::Reserved
                ))
                .count(),
            raw_chunk_count
        );
        for stack in capture
            .spans
            .iter()
            .filter(|span| span.kind == SpanKind::Stack)
        {
            assert!(array(&raw, "tasks").iter().any(|task| {
                number(task, "stack_base_ptr") == stack.start
                    && number(task, "adj_stack_size") == stack.size
            }));
            total_stacks += 1;
        }
        total_sources += capture
            .spans
            .iter()
            .filter(|span| span.source.is_some())
            .count();
    }
    assert!(total_stacks > 0, "require actual task-stack observations");
    let first = &model.snapshots[0];
    let next = &model.snapshots[1];
    let gap = first.time_ns.checked_add(1).unwrap();
    assert!(gap < next.time_ns, "use separated supplied coordinates");
    assert!(model.snapshot_at(gap).is_none());
    for region in &model.regions {
        assert_eq!(
            model.view(&region.id, region.range().unwrap(), gap).runtime,
            TimeConfidence::Unknown
        );
        assert_eq!(model.free_bytes(&region.id, gap), 0);
    }
    println!(
        "Real QEMU ELF/capture import passed: {} points, {total_stacks} stack observations, {total_sources} source-attributed spans; no state inferred between points.",
        model.snapshots.len()
    );
}
