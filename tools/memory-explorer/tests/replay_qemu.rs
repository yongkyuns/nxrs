//! Independent admission of retained isolated-heap QEMU evidence. The fixed
//! qualified ABI is decoded directly from saved header bytes, and the actual
//! ELF's Thumb branch destinations are checked without invoking the Python
//! collector, journal constructor, or its disassembly parser.

use nxrs_memory_explorer::{
    load_heap_journal, load_inputs, load_timing, HeapChunk, HeapChunkState, HeapEvidence,
    HeapRecord, HeapReplay, HeapState, HeapUnknownReason, ReplayCursor, TimingEventKind,
    TimingTrace,
};
use object::{Object, ObjectSection, ObjectSymbol, SymbolKind};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

const NUTTX_REVISION: &str = "2b5c0de5e933281c209f954a63b99cf10c132346";
const APPS_REVISION: &str = "3c772ea35b9245972e18999828f7b533e6fc57cc";
const CLOCK: &str = "qemu-an521-cmsdk-timer1-20mhz";

fn json(path: &Path) -> Value {
    serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
}
fn number(value: &Value, field: &str) -> u64 {
    value[field].as_u64().unwrap()
}
fn text<'a>(value: &'a Value, field: &str) -> &'a str {
    value[field].as_str().unwrap()
}
fn array<'a>(value: &'a Value, field: &str) -> &'a [Value] {
    value[field].as_array().unwrap()
}
fn sha256(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}

fn bytes(hex: &str) -> Vec<u8> {
    assert_eq!(hex.len() % 2, 0);
    (0..hex.len())
        .step_by(2)
        .map(|offset| u8::from_str_radix(&hex[offset..offset + 2], 16).unwrap())
        .collect()
}

fn raw_geometry(event: &Value) -> Vec<HeapChunk> {
    let region = &event["region"];
    let start = number(region, "start");
    let end = start.checked_add(number(region, "size")).unwrap();
    let mut cursor = start;
    let mut previous_free = false;
    let mut previous_size = 0;
    let mut decoded = vec![];
    for raw in array(event, "chunks") {
        assert!(decoded.len() < 256);
        assert_eq!(number(raw, "start"), cursor);
        let header = bytes(text(raw, "header_hex"));
        assert_eq!(header.len(), 48, "qualified ARM default-allocator ABI");
        let preceding = u64::from(u32::from_le_bytes(header[0..4].try_into().unwrap()));
        let flags = u32::from_le_bytes(header[4..8].try_into().unwrap());
        let size = u64::from(flags & !3);
        let free = flags & 1 == 0;
        assert_eq!(flags & 2 != 0, previous_free);
        if previous_free {
            assert_eq!(preceding, previous_size);
        }
        assert!(size >= 48 && size % 8 == 0);
        let next = cursor.checked_add(size).unwrap();
        assert!(next <= end);
        let guard = cursor == start || next == end;
        let state = if guard {
            assert_eq!(size, 48);
            assert!(!free);
            HeapChunkState::Reserved
        } else if free {
            assert!(
                !previous_free,
                "committed adjacent free chunks were not coalesced"
            );
            HeapChunkState::Free
        } else {
            HeapChunkState::Allocated
        };
        assert_eq!(number(raw, "size"), size);
        assert_eq!(
            text(raw, "state"),
            match state {
                HeapChunkState::Reserved => "reserved",
                HeapChunkState::Free => "free",
                HeapChunkState::Allocated => "allocated",
            }
        );
        decoded.push(HeapChunk {
            start: cursor,
            size,
            state,
        });
        cursor = next;
        previous_free = free;
        previous_size = size;
    }
    assert_eq!(cursor, end);
    assert!(decoded.len() >= 3);
    for pointer in array(event, "deferred_payloads") {
        let pointer = pointer.as_u64().unwrap();
        assert!(
            decoded.iter().any(
                |chunk| chunk.state == HeapChunkState::Allocated && chunk.start + 48 == pointer
            ),
            "delayed free is still allocated until actual reclamation"
        );
    }
    decoded
}

fn safe_oracle(event: &Value, expected: &[HeapChunk]) {
    let independent = &event["independent_safe_heap"];
    assert_eq!(
        number(independent, "address"),
        number(event, "heap_address")
    );
    assert_eq!(array(independent, "regions").len(), 1);
    let region = &independent["regions"][0];
    for field in ["start", "size"] {
        assert_eq!(number(region, field), number(&event["region"], field));
    }
    let chunks: Vec<_> = array(region, "chunks")
        .iter()
        .map(|chunk| HeapChunk {
            start: number(chunk, "start"),
            size: number(chunk, "size"),
            state: match text(chunk, "kind") {
                "allocation" => HeapChunkState::Allocated,
                "free" => HeapChunkState::Free,
                "reserved" => HeapChunkState::Reserved,
                other => panic!("unexpected oracle state {other}"),
            },
        })
        .collect();
    assert_eq!(chunks, expected);
    assert_eq!(independent["deferred_payloads"], event["deferred_payloads"]);
}

fn symbol<'a>(file: &'a object::File<'a>, name: &str) -> object::Symbol<'a, 'a> {
    file.symbols()
        .find(|symbol| symbol.name().ok() == Some(name) && !symbol.is_undefined())
        .unwrap_or_else(|| panic!("missing ELF symbol {name}"))
}

fn thumb_destinations(file: &object::File<'_>, address: u64, size: u64) -> Vec<u64> {
    let start = address & !1;
    let section = file
        .sections()
        .find(|section| {
            section.address() <= start
                && start + size <= section.address() + section.size()
                && section.kind() == object::SectionKind::Text
        })
        .unwrap();
    let data = section.data().unwrap();
    let offset = usize::try_from(start - section.address()).unwrap();
    let instructions = &data[offset..offset + usize::try_from(size).unwrap()];
    // ARM mapping symbols distinguish instructions from literal pools, which
    // may be included in a function's ELF size. Never decode pool data as calls.
    let mut mappings: Vec<_> = file
        .symbols()
        .filter_map(|symbol| {
            if symbol.section_index() != Some(section.index()) {
                return None;
            }
            let name = symbol.name().ok()?;
            let code = if name == "$t" || name.starts_with("$t.") {
                true
            } else if name == "$d" || name.starts_with("$d.") {
                false
            } else {
                return None;
            };
            Some((symbol.address(), code))
        })
        .collect();
    mappings.sort_unstable();
    let mut destinations = vec![];
    let mut index = 0;
    while index + 2 <= instructions.len() {
        let address = start + index as u64;
        let mapping = mappings.partition_point(|(location, _)| *location <= address);
        assert!(mapping > 0, "missing ARM code/data mapping at {address:x}");
        if !mappings[mapping - 1].1 {
            index = mappings
                .get(mapping)
                .map_or(instructions.len(), |(location, _)| {
                    usize::try_from(*location - start)
                        .unwrap()
                        .min(instructions.len())
                });
            continue;
        }
        let first = u16::from_le_bytes(instructions[index..index + 2].try_into().unwrap());
        let pc = start + index as u64 + 4;
        if first >> 11 >= 0b11101 {
            assert!(
                index + 4 <= instructions.len(),
                "truncated Thumb instruction at {start:x}, size {size}, offset {index}"
            );
            let second = u16::from_le_bytes(instructions[index + 2..index + 4].try_into().unwrap());
            // Thumb-2 BL T1 and unconditional B.W T4. The admitted Cortex-M
            // allocator calls have direct destinations and no ARM-state BLX.
            if first & 0xf800 == 0xf000 && matches!(second & 0xd000, 0xd000 | 0x9000) {
                let s = u32::from((first >> 10) & 1);
                let i1 = (!(u32::from((second >> 13) & 1) ^ s)) & 1;
                let i2 = (!(u32::from((second >> 11) & 1) ^ s)) & 1;
                let bits = (s << 24)
                    | (i1 << 23)
                    | (i2 << 22)
                    | (u32::from(first & 0x03ff) << 12)
                    | (u32::from(second & 0x07ff) << 1);
                let displacement = ((bits << 7) as i32) >> 7;
                destinations.push(u64::try_from(pc as i64 + i64::from(displacement)).unwrap());
            }
            index += 4;
        } else {
            if first & 0xf800 == 0xe000 {
                let displacement = ((u32::from(first & 0x07ff) << 21) as i32) >> 20;
                destinations.push(u64::try_from(pc as i64 + i64::from(displacement)).unwrap());
            }
            index += 2;
        }
    }
    destinations
}

fn verify_elf_hooks(file: &object::File<'_>) {
    assert_eq!(file.architecture(), object::Architecture::Arm);
    assert!(file.is_little_endian());
    let lock = symbol(file, "__wrap_mm_lock").address() & !1;
    let unlock = symbol(file, "__wrap_mm_unlock").address() & !1;
    let real_lock = symbol(file, "mm_lock").address() & !1;
    let real_unlock = symbol(file, "mm_unlock").address() & !1;
    for (name, locks, unlocks) in [
        ("mm_malloc", 1, 1),
        ("mm_delayfree", 1, 2),
        ("mm_realloc", 1, 3),
        ("mm_memalign", 1, 1),
    ] {
        let function = symbol(file, name);
        let calls = thumb_destinations(file, function.address(), function.size());
        assert_eq!(
            calls.iter().filter(|target| **target == lock).count(),
            locks,
            "{name} lock coverage"
        );
        assert_eq!(
            calls.iter().filter(|target| **target == unlock).count(),
            unlocks,
            "{name} unlock coverage"
        );
    }
    for function in file.symbols().filter(|symbol| {
        symbol.kind() == SymbolKind::Text && !symbol.is_undefined() && symbol.size() > 0
    }) {
        let name = function.name().unwrap();
        let calls = thumb_destinations(file, function.address(), function.size());
        let locks = calls.iter().filter(|target| **target == real_lock).count();
        let unlocks = calls
            .iter()
            .filter(|target| **target == real_unlock)
            .count();
        assert_eq!(
            locks,
            usize::from(name == "__wrap_mm_lock"),
            "unexpected direct real mm_lock call from {name}"
        );
        assert_eq!(
            unlocks,
            usize::from(name == "__wrap_mm_unlock"),
            "unexpected direct real mm_unlock call from {name}"
        );
    }
    assert!(symbol(file, "replay_probe_stop").size() > 0);
    assert!(symbol(file, "heap_replay_main").size() > 0);
}

fn check_replay_point(
    replay: &HeapReplay,
    event: &Value,
    expected: &[HeapChunk],
    evidence: HeapEvidence,
) {
    let points: Vec<_> = replay
        .points
        .iter()
        .filter(|point| {
            point.sequence == number(event, "sequence")
                && point.time_ns == number(event, "time_ns")
                && point.evidence == evidence
        })
        .collect();
    assert_eq!(
        points.len(),
        1,
        "one exact ordered journal point per retained native observation"
    );
    let HeapState::Known(state) =
        replay.heap_state_at(&replay.journal.heap.id, ReplayCursor::Point(points[0].id))
    else {
        panic!("proven native point imported as unknown")
    };
    assert_eq!(state.chunks, expected);
    assert!(state
        .spans()
        .iter()
        .all(|span| span.source.is_none() && span.backtrace.is_empty()));
}

fn contained(chunks: &[HeapChunk], pointer: u64) -> &HeapChunk {
    chunks
        .iter()
        .find(|chunk| chunk.state == HeapChunkState::Allocated && chunk.start + 48 == pointer)
        .unwrap()
}

fn check_trace_instant(
    trace: &TimingTrace,
    event: &Value,
    evidence: &str,
    heap_id: &str,
    epoch: &str,
) {
    let origin = trace.metadata.origin_ns.unwrap();
    let matches: Vec<_> = trace
        .events
        .iter()
        .filter(|item| {
            item.kind == TimingEventKind::Instant
                && item.category == "heap.observation"
                && item.start_ns == number(event, "time_ns") - origin
                && item.args["sequence"] == number(event, "sequence")
                && item.args["evidence"] == evidence
        })
        .collect();
    assert_eq!(
        matches.len(),
        1,
        "missing or duplicate derived observation instant"
    );
    assert_eq!(matches[0].args["heap_id"], heap_id);
    assert_eq!(matches[0].args["heap_epoch"], epoch);
}

#[test]
#[ignore = "requires actual QEMU capture via NXRS_HEAP_REPLAY_BUILD and NXRS_HEAP_REPLAY_EVIDENCE"]
fn real_qemu_heap_journal_matches_native_headers_and_every_sequence() {
    let build_dir = PathBuf::from(
        std::env::var_os("NXRS_HEAP_REPLAY_BUILD")
            .expect("set NXRS_HEAP_REPLAY_BUILD to the build evidence directory"),
    );
    let run_dir = PathBuf::from(
        std::env::var_os("NXRS_HEAP_REPLAY_EVIDENCE")
            .expect("set NXRS_HEAP_REPLAY_EVIDENCE to the capture directory"),
    );
    let build = json(&build_dir.join("build.json"));
    let report = json(&run_dir.join("report.json"));
    let raw = json(&run_dir.join("raw-events.json"));
    let elf = build_dir.join("nuttx");
    let elf_bytes = fs::read(&elf).unwrap();
    let object = object::File::parse(elf_bytes.as_slice()).unwrap();
    let elf_hash = sha256(&elf);
    for receipt in [&build, &report, &raw] {
        assert_eq!(text(receipt, "elf_sha256"), elf_hash);
        assert_eq!(number(receipt, "schema_version"), 1);
    }
    assert_eq!(text(&build, "nuttx_revision"), NUTTX_REVISION);
    assert_eq!(text(&build, "apps_revision"), APPS_REVISION);
    for name in ["nuttx", "apps"] {
        assert_eq!(
            build["source_identity"][name]["tracked_sources_clean"],
            true
        );
    }
    assert_eq!(
        text(&build, "config_sha256"),
        sha256(&build_dir.join("resolved.config"))
    );
    assert_eq!(
        text(&report, "config_sha256"),
        text(&build, "config_sha256")
    );
    assert_eq!(
        text(&build, "disassembly_sha256"),
        sha256(&build_dir.join("disassembly.txt"))
    );
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for (name, hash) in build["fixture_sha256"].as_object().unwrap() {
        assert!(!name.contains('/') && !name.contains('\\'));
        assert_eq!(
            hash.as_str().unwrap(),
            sha256(&repo.join("tests/nuttx-std/heap-replay").join(name)),
            "fixture source changed after native build"
        );
    }
    assert_eq!(build["fixture_sha256"].as_object().unwrap().len(), 4);
    assert_eq!(
        text(&build, "shared_clock_sha256"),
        sha256(&repo.join("tests/nuttx-std/memory-timing/qemu_timer_clock.c"))
    );
    let configuration = fs::read_to_string(build_dir.join("resolved.config")).unwrap();
    for line in [
        "CONFIG_ARCH_BOARD_MPS2_AN521=y",
        "CONFIG_DEBUG_SYMBOLS=y",
        "CONFIG_DEBUG_NOOPT=y",
        "CONFIG_LTO_NONE=y",
        "CONFIG_EXAMPLES_HEAP_REPLAY=y",
        "CONFIG_MM_DEFAULT_MANAGER=y",
        "CONFIG_BUILD_FLAT=y",
        "CONFIG_MM_REGIONS=1",
        "CONFIG_MM_BACKTRACE=8",
        "CONFIG_MM_FREE_DELAYCOUNT_MAX=0",
        "CONFIG_MM_DEFAULT_ALIGNMENT=8",
        "CONFIG_MM_NODE_GUARDSIZE=0",
    ] {
        assert!(
            configuration.lines().any(|actual| actual == line),
            "missing {line}"
        );
    }
    for name in [
        "SMP",
        "MM_HEAP_MEMPOOL",
        "MM_TLSF_MANAGER",
        "MM_KERNEL_HEAP",
    ] {
        assert!(
            !configuration
                .lines()
                .any(|line| line == format!("CONFIG_{name}=y")),
            "unsupported allocator configuration {name}"
        );
    }
    verify_elf_hooks(&object);
    let arena = symbol(&object, "g_replay_arena");
    assert_eq!(arena.size(), 65536);
    assert_eq!(arena.address() % 64, 0);
    assert_eq!(number(&build, "static_arena_bytes"), arena.size());
    assert_eq!(number(&build, "transaction_bound"), 256);

    let layout = Path::new(env!("CARGO_MANIFEST_DIR")).join("layouts/mps2-an521-qemu.json");
    let model = load_inputs(&elf, None, &[], Some(&layout)).unwrap();
    let trace = load_timing(&run_dir.join("timing.json")).unwrap();
    let replay = load_heap_journal(&run_dir.join("journal.json"), &model, Some(&trace)).unwrap();
    assert_eq!(
        text(&report, "timing_sha256"),
        sha256(&run_dir.join("timing.json"))
    );
    assert_eq!(
        trace.metadata.origin_ns,
        Some(replay.journal.initial.time_ns)
    );
    assert_eq!(trace.metadata.complete, Some(true));
    assert_eq!(trace.metadata.lost_records, Some(0));
    assert!(trace
        .metadata
        .source
        .as_deref()
        .unwrap()
        .starts_with("derived-stopped-heap-journal;"));
    assert_eq!(replay.journal.provenance.nuttx_revision, NUTTX_REVISION);
    assert_eq!(
        replay.journal.provenance.config_sha256,
        text(&build, "config_sha256")
    );
    assert_eq!(
        replay.journal.provenance.collector,
        "nuttx-default-heap-lock-probes-v1"
    );
    assert_eq!(replay.journal.clock, CLOCK);
    assert_eq!(text(&report, "clock"), CLOCK);
    assert_eq!(replay.journal.recording_id, text(&raw, "recording_id"));
    assert_eq!(replay.journal.recording_id, text(&report, "recording_id"));
    assert_eq!(raw["target_function_calls"], false);
    assert_eq!(
        raw["header_layout"],
        serde_json::json!({"size":48,"fields":{"preceding":{"offset":0,"size":4},"size":{"offset":4,"size":4}},"allocated_mask":1,"previous_free_mask":2,"byte_order":"little"})
    );
    let events = array(&raw, "events");
    assert!(events.len() <= 538);
    let initial = &events[0];
    assert_eq!(text(initial, "kind"), "initial");
    assert_eq!(number(initial, "sequence"), 0);
    let baseline = raw_geometry(initial);
    assert_eq!(replay.journal.initial.chunks, baseline);
    assert_eq!(replay.journal.initial.time_ns, number(initial, "time_ns"));
    assert_eq!(
        replay.journal.heap.start,
        number(&initial["region"], "start")
    );
    assert_eq!(replay.journal.heap.size, number(&initial["region"], "size"));
    assert_eq!(
        replay.journal.heap.start + replay.journal.heap.size,
        arena.address() + arena.size()
    );
    assert_eq!(number(initial, "heap_address"), arena.address());
    let prefix = replay.journal.heap.start - arena.address();
    assert!(prefix > 0 && prefix < arena.size());
    let owner = number(initial, "task_id");
    assert!(owner > 0);
    let mut current = baseline.clone();
    let mut sequence = 0;
    let mut safe_count = 0;
    let mut previous_time = 0;
    let mut pending: Option<&Value> = None;
    let mut records = replay.journal.records.iter();
    let mut safe_points = BTreeMap::new();
    let mut operation_commits: BTreeMap<u64, usize> = BTreeMap::new();
    for (index, event) in events.iter().enumerate() {
        let time = number(event, "time_ns");
        let kind = text(event, "kind");
        let geometry = raw_geometry(event);
        assert_eq!(number(event, "probe_sequence"), index as u64 + 1);
        assert_eq!(event["region"], initial["region"]);
        assert_eq!(number(event, "heap_address"), arena.address());
        assert_eq!(number(event, "arena_start"), arena.address());
        assert_eq!(number(event, "arena_bytes"), arena.size());
        assert_eq!(number(event, "metadata_prefix_bytes"), prefix);
        assert_eq!(number(event, "task_id"), owner);
        assert_eq!(number(event, "primask"), 1);
        assert_ne!(number(event, "basepri"), 0);
        assert_eq!(number(event, "xpsr") & 0x1ff, 0);
        assert_eq!(number(event, "faults"), 0);
        assert_eq!(event["metadata_unchanged"], true);
        assert_eq!(event["timer_unchanged"], true);
        assert_eq!(time, number(event, "ticks").checked_mul(50).unwrap());
        assert!(previous_time <= time && time < 5_000_000_000);
        assert!(number(event, "timer_ticks") >= number(event, "ticks"));
        previous_time = time;
        match kind {
            "begin" => {
                assert!(pending.is_none());
                assert_eq!(geometry, current);
                assert_eq!(number(event, "sequence"), sequence);
                assert_eq!(number(event, "begin_count"), sequence + 1);
                assert_eq!(number(event, "commit_count"), sequence);
                assert_eq!(number(event, "operation"), safe_count + 1);
                assert_eq!(number(event, "mutex_holder") & 0x7fffffff, owner);
                pending = Some(event);
            }
            "commit" => {
                let begin = pending.take().expect("commit has no matching acquisition");
                sequence += 1;
                assert_eq!(number(event, "sequence"), sequence);
                assert_eq!(number(event, "begin_count"), sequence);
                assert_eq!(number(event, "commit_count"), sequence);
                assert_eq!(number(event, "operation"), number(begin, "operation"));
                assert_eq!(number(event, "mutex_holder") & 0x7fffffff, owner);
                let HeapRecord::Transaction {
                    sequence: record_sequence,
                    heap_epoch,
                    start_ns,
                    commit_ns,
                    start,
                    size,
                    expected,
                    replacement,
                    ..
                } = records.next().unwrap()
                else {
                    panic!("missing journal transaction")
                };
                assert_eq!(*record_sequence, sequence);
                assert_eq!(heap_epoch, &replay.journal.heap.epoch);
                assert_eq!(*start_ns, number(begin, "time_ns"));
                assert_eq!(*commit_ns, time);
                let windows: Vec<_> = trace
                    .events
                    .iter()
                    .filter(|item| {
                        item.kind == TimingEventKind::Interval
                            && item.category == "heap.mutation"
                            && item.args["sequence"] == sequence
                    })
                    .collect();
                assert_eq!(
                    windows.len(),
                    1,
                    "one derived window per native committed sequence"
                );
                assert_eq!(
                    windows[0].start_ns,
                    number(begin, "time_ns") - replay.journal.initial.time_ns
                );
                assert_eq!(windows[0].end_ns, time - replay.journal.initial.time_ns);
                assert_eq!(windows[0].args["source_start_ns"], number(begin, "time_ns"));
                assert_eq!(windows[0].args["source_commit_ns"], time);
                assert_eq!(windows[0].args["heap_id"], replay.journal.heap.id);
                check_trace_instant(
                    &trace,
                    event,
                    "reconstructed commit boundary",
                    &replay.journal.heap.id,
                    &replay.journal.heap.epoch,
                );
                let end = start.checked_add(*size).unwrap();
                let before: Vec<_> = current
                    .iter()
                    .filter(|chunk| *start <= chunk.start && chunk.start + chunk.size <= end)
                    .cloned()
                    .collect();
                let after: Vec<_> = geometry
                    .iter()
                    .filter(|chunk| *start <= chunk.start && chunk.start + chunk.size <= end)
                    .cloned()
                    .collect();
                assert_eq!(*expected, before);
                assert_eq!(*replacement, after);
                check_replay_point(&replay, event, &geometry, HeapEvidence::Reconstructed);
                *operation_commits
                    .entry(number(event, "operation"))
                    .or_default() += 1;
                current = geometry.clone();
            }
            "safe" => {
                assert!(pending.is_none());
                safe_count += 1;
                assert_eq!(number(event, "operation"), safe_count);
                assert_eq!(geometry, current);
                let HeapRecord::Checkpoint {
                    heap_epoch,
                    sequence: record_sequence,
                    time_ns,
                    chunks,
                    ..
                } = records.next().unwrap()
                else {
                    panic!("missing independent journal checkpoint")
                };
                assert_eq!(heap_epoch, &replay.journal.heap.epoch);
                assert_eq!(*record_sequence, sequence);
                assert_eq!(*time_ns, time);
                assert_eq!(*chunks, geometry);
                check_replay_point(&replay, event, &geometry, HeapEvidence::ObservedCheckpoint);
                check_trace_instant(
                    &trace,
                    event,
                    "observed checkpoint",
                    &replay.journal.heap.id,
                    &replay.journal.heap.epoch,
                );
                safe_points.insert(safe_count, (event, geometry.clone()));
            }
            "initial" => {
                assert_eq!(index, 0);
                assert_eq!(geometry, baseline);
                check_replay_point(&replay, event, &geometry, HeapEvidence::ObservedCheckpoint);
                check_trace_instant(
                    &trace,
                    event,
                    "observed checkpoint",
                    &replay.journal.heap.id,
                    &replay.journal.heap.epoch,
                );
            }
            "final" => {
                assert_eq!(index + 1, events.len());
                assert!(pending.is_none());
                assert_eq!(geometry, baseline);
                let closing = replay.journal.closing.as_ref().unwrap();
                assert_eq!(closing.heap_epoch, replay.journal.heap.epoch);
                assert_eq!(closing.sequence, sequence);
                assert_eq!(closing.time_ns, time);
                check_trace_instant(
                    &trace,
                    event,
                    "coverage receipt",
                    &replay.journal.heap.id,
                    &replay.journal.heap.epoch,
                );
            }
            other => panic!("unexpected native kind {other}"),
        }
        assert_eq!(number(event, "safe_count"), safe_count);
        if matches!(kind, "initial" | "safe" | "final") {
            assert_eq!(number(event, "mutex_holder"), 0x7ffffffe);
            assert_eq!(number(event, "sequence"), sequence);
            assert_eq!(number(event, "begin_count"), sequence);
            assert_eq!(number(event, "commit_count"), sequence);
            safe_oracle(event, &geometry);
        }
    }
    assert!(records.next().is_none());
    assert_eq!(sequence, 29);
    assert_eq!(safe_count, 24);
    assert_eq!(events.len(), 84);
    assert_eq!(
        trace
            .events
            .iter()
            .filter(|event| event.kind == TimingEventKind::Interval)
            .count(),
        29
    );
    assert_eq!(
        trace
            .events
            .iter()
            .filter(|event| event.kind == TimingEventKind::Instant)
            .count(),
        55
    );
    assert_eq!(
        replay.points.len(),
        1 + sequence as usize + safe_count as usize
    );
    assert_eq!(number(&report, "transactions"), sequence);
    assert_eq!(number(&report, "safe_points"), safe_count);
    let console = fs::read_to_string(run_dir.join("console.log")).unwrap();
    let lines: Vec<_> = console
        .lines()
        .filter(|line| line.starts_with("NXRS_REPLAY "))
        .collect();
    assert_eq!(lines.len(), 1);
    assert!(lines[0].starts_with("NXRS_REPLAY PASS "));
    let footer: BTreeMap<_, _> = lines[0]
        .split_whitespace()
        .filter_map(|word| word.split_once('='))
        .collect();
    assert_eq!(footer["recording_id"], replay.journal.recording_id);
    for (field, expected) in [
        ("sequence", sequence),
        ("begins", sequence),
        ("commits", sequence),
        ("safe_points", safe_count),
        ("probes", events.len() as u64),
        ("faults", 0),
        ("arena_bytes", arena.size()),
        ("owner", owner),
    ] {
        assert_eq!(footer[field].parse::<u64>().unwrap(), expected);
    }

    // Workload properties are checked from raw addresses and extents, not from
    // journal labels or the Python report's success booleans.
    let pointers = |operation: u64| {
        array(safe_points[&operation].0, "blocks")
            .iter()
            .map(|value| value.as_u64().unwrap())
            .collect::<Vec<_>>()
    };
    let chunks = |operation: u64| &safe_points[&operation].1;
    let first: Vec<_> = pointers(3)[..3]
        .iter()
        .map(|pointer| contained(chunks(3), *pointer))
        .collect();
    assert!(first
        .windows(2)
        .all(|pair| pair[0].start + pair[0].size == pair[1].start));
    assert!(chunks(4).iter().any(|chunk| chunk.start == first[1].start
        && chunk.size == first[1].size
        && chunk.state == HeapChunkState::Free));
    for operation in [8, 16, 18, 20, 24] {
        assert_eq!(chunks(operation), &baseline);
    }
    let old = pointers(9)[0];
    assert_eq!(pointers(10)[0], old);
    assert_eq!(pointers(11)[0], old);
    let sizes: Vec<_> = [9, 10, 11]
        .into_iter()
        .map(|operation| contained(chunks(operation), old).size)
        .collect();
    assert!(sizes[1] < sizes[2] && sizes[2] < sizes[0]);
    assert_ne!(pointers(13)[0], old);
    assert!(operation_commits[&13] >= 3);
    assert_eq!(chunks(14), chunks(13));
    assert_eq!(pointers(17)[0] % 256, 0);
    assert!(operation_commits[&17] >= 2);
    assert_eq!(pointers(19)[0], pointers(21)[0]);
    assert_eq!(chunks(22), chunks(21));
    assert_eq!(
        array(safe_points[&22].0, "deferred_payloads"),
        [serde_json::json!(pointers(21)[0])]
    );
    assert!(array(safe_points[&23].0, "deferred_payloads").is_empty());
    assert!(operation_commits[&23] >= 2);

    let loss = load_heap_journal(
        &run_dir.join("journal-loss-control.json"),
        &model,
        Some(&trace),
    )
    .unwrap();
    let control = json(&run_dir.join("loss-control.json"));
    assert_eq!(text(&control, "kind"), "injected_missing_cancelling_pair");
    let dropped: Vec<_> = array(&control, "dropped_sequences")
        .iter()
        .map(|value| value.as_u64().unwrap())
        .collect();
    assert_eq!(dropped, [23, 24]);
    let state = |recording: &HeapReplay, sequence| match recording
        .heap_state_at(&recording.journal.heap.id, ReplayCursor::Sequence(sequence))
    {
        HeapState::Known(state) => state.chunks.to_vec(),
        other => panic!("unproved native state {other:?}"),
    };
    assert_eq!(
        state(&replay, 22),
        state(&replay, 24),
        "the removed pair must cancel geometrically"
    );
    assert_ne!(state(&replay, 22), state(&replay, 23));
    assert!(
        matches!(loss.heap_state_at(&loss.journal.heap.id, ReplayCursor::Time(number(&control,"unknown_probe_time_ns"))), HeapState::Unknown(state) if state.reason == HeapUnknownReason::SequenceGap)
    );
    let recovery = number(&control, "recovery_checkpoint_sequence");
    assert_eq!(state(&loss, recovery), state(&replay, recovery));
    assert!(
        matches!(loss.heap_state_at(&loss.journal.heap.id, ReplayCursor::Time(number(&control,"recovery_checkpoint_time_ns"))), HeapState::Known(state) if state.evidence == HeapEvidence::ObservedCheckpoint)
    );
    println!("Independent QEMU replay gate passed: {sequence} transactions, {safe_count} safe points, {} native probes; all raw chunk headers, actual ELF mutex destinations, UART sequence closure, controlled geometry, derived timing and cancelling-pair loss recovery verified.",events.len());
}
