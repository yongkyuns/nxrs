use nxrs_memory_explorer::*;
use std::path::Path;

fn board() -> LayoutFile {
    parse_layout(include_str!("../layouts/mps2-an521-qemu.json")).unwrap()
}

fn empty_board() -> MemoryModel {
    let layout = board();
    MemoryModel::from_snapshot(SnapshotFile {
        schema_version: 1,
        build: BuildIdentity {
            elf_sha256: "0".repeat(64),
            label: "synthetic board contract".into(),
        },
        regions: layout.regions,
        physical_resources: layout.physical_resources,
        snapshots: vec![Snapshot {
            time_ns: 0,
            label: "synthetic point".into(),
            spans: vec![],
            complete_regions: vec![],
        }],
        warnings: layout.warnings,
        capture: None,
    })
    .unwrap()
}

fn observed(
    id: &str,
    region: &str,
    start: u64,
    size: u64,
    kind: SpanKind,
    parent: Option<&str>,
) -> Span {
    Span {
        id: id.into(),
        name: id.into(),
        region: region.into(),
        start,
        size,
        kind,
        source: None,
        parent: parent.map(str::to_owned),
        detail: None,
        backtrace: vec![],
    }
}

#[test]
fn verified_descriptor_declares_ram_capacity_once_and_unknown_is_not_free() {
    let model = empty_board();
    assert_eq!(
        model.physical_resources.iter().map(|r| r.size).sum::<u64>(),
        24 * 1024 * 1024 + 128 * 1024
    );
    assert_eq!(model.physical_resources.len(), 7);
    assert_eq!(
        model
            .physical_resources
            .iter()
            .map(|r| r.banks.len())
            .sum::<usize>(),
        8
    );
    assert!(model.regions.iter().all(|r| r.kind == RegionKind::Ram));
    let ssram23 = model
        .physical_resources
        .iter()
        .find(|r| r.id == "ssram23")
        .unwrap();
    assert_eq!(ssram23.size, 0x400000);
    assert_eq!(ssram23.banks[0].offset, 0);
    assert_eq!(ssram23.banks[0].size, 0x200000);
    assert_eq!(ssram23.banks[1].offset, 0x200000);
    assert_eq!(ssram23.banks[1].size, 0x200000);
    assert!(model.physical_occupied_bytes(0).is_empty());
    for region in &model.regions {
        assert_eq!(model.free_bytes(&region.id, 0), 0);
        assert_eq!(model.occupied_bytes(&region.id, 0), 0);
    }
    // Every declared load window has an explicitly matching CPU window; their
    // identical numeric starts alone are not used to establish this mapping.
    for load in model
        .regions
        .iter()
        .filter(|r| r.domain == AddressDomain::LinkedLoad)
    {
        let cpu = model
            .regions
            .iter()
            .find(|r| r.domain == AddressDomain::CpuVirtual && r.start == load.start)
            .unwrap();
        assert_eq!(load.physical, cpu.physical);
        assert_eq!(load.size, cpu.size);
    }
    assert!(model
        .warnings
        .iter()
        .any(|warning| warning.contains("QEMU v10.0.13")));
}

#[test]
fn four_code_aliases_share_storage_and_heap_chunks_can_cross_bank_boundaries() {
    let mut model = empty_board();
    for (index, (region, start)) in [
        ("cpu-ssram0-ns", 0),
        ("cpu-ssram0-ns-mirror", 0x00400000),
        ("cpu-ssram0-secure", 0x10000000),
        ("cpu-ssram0-secure-mirror", 0x10400000),
    ]
    .into_iter()
    .enumerate()
    {
        model.snapshots[0].spans.push(observed(
            &format!("alias-{index}"),
            region,
            start + 128,
            64,
            SpanKind::Allocation,
            None,
        ));
    }
    model.snapshots[0].spans.extend([
        observed(
            "arena",
            "cpu-ssram23-secure",
            0x381fff00,
            0x20000,
            SpanKind::HeapArena,
            None,
        ),
        observed(
            "free",
            "cpu-ssram23-secure",
            0x381fff00,
            0x20000,
            SpanKind::Free,
            Some("arena"),
        ),
    ]);
    model.snapshots[0]
        .complete_regions
        .push("cpu-ssram23-secure".into());
    model.validate().unwrap();
    assert_eq!(model.physical_occupied_bytes(0)["ssram0"], 64);
    let stats = model.heap_statistics("cpu-ssram23-secure", 0);
    assert_eq!(stats.len(), 1);
    assert_eq!(stats[0].largest_free_chunk, 0x20000);
    assert_eq!(stats[0].free_chunk_bytes, 0x20000);
    assert_eq!(stats[0].unknown_bytes, 0);
    assert_eq!(
        model
            .physical_intervals(0)
            .iter()
            .filter(|i| i.resource == "ssram23" && i.span.kind == SpanKind::Free)
            .count(),
        1
    );
}

#[test]
fn malformed_bank_partitions_and_mapping_capacities_are_rejected() {
    let valid = serde_json::to_value(board()).unwrap();
    for bad in [
        {
            let mut v = valid.clone();
            v["physical_resources"][1]["banks"][1]["offset"] = 0.into();
            v
        },
        {
            let mut v = valid.clone();
            v["physical_resources"][1]["banks"][1]["size"] = 1.into();
            v
        },
        {
            let mut v = valid.clone();
            v["physical_resources"][1]["banks"][1]["offset"] = u64::MAX.into();
            v
        },
        {
            let mut v = valid.clone();
            v["physical_resources"][1]["banks"][0]["size"] = 0.into();
            v
        },
        {
            let mut v = valid.clone();
            v["physical_resources"][1]["banks"][1]["name"] =
                v["physical_resources"][1]["banks"][0]["name"].clone();
            v
        },
        {
            let mut v = valid.clone();
            v["regions"][0]["physical"]["offset"] = 1.into();
            v
        },
        {
            let mut v = valid.clone();
            let mut r = v["regions"][15].clone();
            r["id"] = "overlapping-linked-load".into();
            v["regions"].as_array_mut().unwrap().push(r);
            v
        },
    ] {
        assert!(parse_layout(&bad.to_string()).is_err());
    }
}

#[test]
#[ignore = "requires real qualify_qemu.py evidence and matching ARM ELF in explicit environment paths"]
fn captured_mps2_heap_merges_into_board_without_changing_allocator_geometry() {
    let evidence =
        std::env::var("NXRS_MEMORY_QEMU_EVIDENCE").expect("set NXRS_MEMORY_QEMU_EVIDENCE");
    let elf = std::env::var("NXRS_MEMORY_QEMU_ELF").expect("set NXRS_MEMORY_QEMU_ELF");
    assert!(Path::new(&evidence).is_absolute() && Path::new(&elf).is_absolute());
    let captures: Vec<_> = (0..3)
        .map(|index| Path::new(&evidence).join(format!("capture-{index}.json")))
        .collect();
    let without_layout = load_inputs(Path::new(&elf), None, &captures, None).unwrap();
    let layout = Path::new(env!("CARGO_MANIFEST_DIR")).join("layouts/mps2-an521-qemu.json");
    let model = load_inputs(Path::new(&elf), None, &captures, Some(&layout)).unwrap();
    let data = model
        .load_mappings
        .iter()
        .find(|m| m.section == ".data")
        .unwrap();
    assert_eq!(data.runtime.start, 0x38000000);
    assert!(data.distinct_load_address());
    assert_eq!(data.runtime.region, "cpu-ssram23-secure");
    assert_eq!(data.load.region, "load-ssram0-secure");
    assert!(model.load_mappings.iter().all(|m| m.section != ".bss"));
    assert!(model
        .static_spans
        .iter()
        .filter(|s| s.name == ".bss")
        .all(|s| s.region == "cpu-ssram23-secure"));
    for (before, after) in without_layout.snapshots.iter().zip(&model.snapshots) {
        assert_eq!(before.spans.len(), after.spans.len());
        assert_eq!(before.time_ns, after.time_ns);
        for (a, b) in before.spans.iter().zip(&after.spans) {
            assert_eq!(
                (a.id.as_str(), a.start, a.size, a.parent.as_deref()),
                (b.id.as_str(), b.start, b.size, b.parent.as_deref())
            );
            assert_eq!(a.backtrace, b.backtrace);
        }
        let old_arena = before
            .spans
            .iter()
            .find(|s| s.kind == SpanKind::HeapArena)
            .unwrap();
        let new_arena = after.spans.iter().find(|s| s.id == old_arena.id).unwrap();
        assert!(new_arena.start < 0x38200000 && new_arena.end() == 0x38400000);
        assert_eq!(new_arena.region, "cpu-ssram23-secure");
        let old_stats = without_layout.heap_statistics(&old_arena.region, before.time_ns);
        let new_stats = model.heap_statistics(&new_arena.region, after.time_ns);
        assert_eq!(old_stats.len(), new_stats.len());
        for (a, b) in old_stats.iter().zip(&new_stats) {
            assert_eq!(
                (
                    a.arena_bytes,
                    a.allocated_chunk_bytes,
                    a.free_chunk_bytes,
                    a.largest_free_chunk,
                    a.unknown_bytes
                ),
                (
                    b.arena_bytes,
                    b.allocated_chunk_bytes,
                    b.free_chunk_bytes,
                    b.largest_free_chunk,
                    b.unknown_bytes
                )
            );
        }
        assert_eq!(model.free_bytes("cpu-system-ram", after.time_ns), 0);
        assert!(!model
            .physical_occupied_bytes(after.time_ns)
            .contains_key("system-ram"));
    }
    println!("Qualified AN521 board projection: .data LMA {:#x} -> VMA {:#x}, {} bytes; {} captures retain exact heap geometry and source frames", data.load.start, data.runtime.start, data.runtime.size, model.snapshots.len());
}
