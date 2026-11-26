use nxrs_memory_explorer::*;

fn region(id: &str, start: u64, size: u64) -> Region {
    Region {
        id: id.into(),
        name: id.into(),
        start,
        size,
        kind: RegionKind::Ram,
        domain: AddressDomain::CpuVirtual,
        physical: None,
    }
}

fn span(id: &str, start: u64, size: u64, kind: SpanKind, parent: Option<&str>) -> Span {
    Span {
        id: id.into(),
        name: id.into(),
        region: "ram".into(),
        start,
        size,
        kind,
        source: None,
        parent: parent.map(str::to_owned),
        detail: None,
        backtrace: vec![],
    }
}

fn observation(spans: Vec<Span>) -> SnapshotFile {
    SnapshotFile {
        schema_version: 1,
        build: BuildIdentity {
            elf_sha256: "a".repeat(64),
            label: "fixture".into(),
        },
        regions: vec![region("ram", 0x1000, 0x1000)],
        physical_resources: vec![],
        snapshots: vec![Snapshot {
            time_ns: 10,
            label: "observed point".into(),
            spans,
            complete_regions: vec![],
        }],
        capture: None,
        warnings: vec![],
    }
}

#[test]
fn integer_intervals_do_not_overflow_or_round_high_addresses() {
    assert!(AddressRange::new(u64::MAX - 1, 2).is_err());
    assert!(AddressRange::new(0, 0).is_err());
    let range = AddressRange::new((1_u64 << 60) + 3, 7).unwrap();
    assert_eq!(range.size(), 7);
    assert_eq!(range.start, 1_152_921_504_606_846_979);
    assert!(!range.overlaps(AddressRange::new(range.end, 1).unwrap()));
}

#[test]
fn recorded_backtraces_preserve_unresolved_pcs_and_old_captures_remain_valid() {
    let mut value = serde_json::to_value(observation(vec![span(
        "allocation",
        0x1000,
        32,
        SpanKind::Allocation,
        None,
    )]))
    .unwrap();
    value["snapshots"][0]["spans"][0]
        .as_object_mut()
        .unwrap()
        .remove("backtrace");
    assert!(
        parse_snapshot(&value.to_string()).unwrap().snapshots[0].spans[0]
            .backtrace
            .is_empty()
    );
    value["snapshots"][0]["spans"][0]["backtrace"] = serde_json::json!([
        {"pc":1234,"function":"allocate","file":"source.rs","line":17},
        {"pc":u64::MAX,"function":null,"file":null,"line":null}
    ]);
    let file = parse_snapshot(&value.to_string()).unwrap();
    let frames = &file.snapshots[0].spans[0].backtrace;
    assert_eq!(frames[0].line, Some(17));
    assert_eq!(frames[1].pc, u64::MAX);
    assert!(frames[1].function.is_none() && frames[1].file.is_none() && frames[1].line.is_none());
    value["snapshots"][0]["spans"][0]["backtrace"][1]["pc"] = (-1).into();
    assert!(parse_snapshot(&value.to_string()).is_err());
}

#[test]
fn nested_stack_and_chunk_are_counted_once() {
    let file = observation(vec![
        span("arena", 0x1000, 0x1000, SpanKind::HeapArena, None),
        span(
            "allocation",
            0x1000,
            0x200,
            SpanKind::Allocation,
            Some("arena"),
        ),
        span("stack", 0x1010, 0x1e0, SpanKind::Stack, Some("allocation")),
        span("used", 0x1100, 0xf0, SpanKind::StackUsed, Some("stack")),
    ]);
    let model = MemoryModel::from_snapshot(file).unwrap();
    assert_eq!(model.occupied_bytes("ram", 10), 0x200);
    assert_eq!(model.free_bytes("ram", 10), 0);
    let stats = model.heap_statistics("ram", 10);
    assert_eq!(stats[0].unknown_bytes, 0xe00);
    assert_eq!(stats[0].allocated_chunk_bytes, 0x200);
}

#[test]
fn unowned_gap_is_unknown_not_free() {
    let file = observation(vec![
        span("arena", 0x1000, 0x1000, SpanKind::HeapArena, None),
        span("free", 0x1000, 0x100, SpanKind::Free, Some("arena")),
    ]);
    let model = MemoryModel::from_snapshot(file).unwrap();
    assert_eq!(model.free_bytes("ram", 10), 0x100);
    assert_eq!(model.occupied_bytes("ram", 10), 0);
    assert_eq!(model.heap_statistics("ram", 10)[0].unknown_bytes, 0xf00);
}

#[test]
fn flat_physical_projection_leaves_partial_arena_and_pool_gaps_unknown() {
    let mut file = observation(vec![
        span("arena", 0x1000, 0x1000, SpanKind::HeapArena, None),
        span(
            "allocation",
            0x1000,
            0x800,
            SpanKind::Allocation,
            Some("arena"),
        ),
        span("pool", 0x1100, 0x300, SpanKind::Pool, Some("allocation")),
        span("pool-free", 0x1100, 0x80, SpanKind::Free, Some("pool")),
        span(
            "pool-used",
            0x1180,
            0x80,
            SpanKind::Allocation,
            Some("pool"),
        ),
        span("heap-free", 0x1800, 0x100, SpanKind::Free, Some("arena")),
    ]);
    file.physical_resources.push(PhysicalResource {
        id: "sram".into(),
        name: "SRAM".into(),
        size: 0x1000,
        banks: vec![],
    });
    file.regions[0].physical = Some(PhysicalBacking {
        id: "sram".into(),
        offset: 0,
    });
    let mut alias = region("alias", 0x3000, 0x1000);
    alias.physical = file.regions[0].physical.clone();
    file.regions.push(alias);
    let mut model = MemoryModel::from_snapshot(file).unwrap();
    let mut reservation = span("static-storage", 0x3000, 0x1000, SpanKind::Bss, None);
    reservation.region = "alias".into();
    model.static_spans.push(reservation);
    model.validate().unwrap();
    let intervals = model.physical_occupied_intervals(10);
    let mut ranges: Vec<_> = intervals
        .iter()
        .map(|interval| (interval.range.start, interval.range.end))
        .collect();
    ranges.sort_unstable();
    assert_eq!(ranges, vec![(0, 0x100), (0x180, 0x200), (0x400, 0x800)]);
    assert_eq!(model.physical_occupied_bytes(10)["sram"], 0x580);
    assert!(intervals
        .iter()
        .all(|interval| interval.span.kind == SpanKind::Allocation));
    let original = model.snapshots[0]
        .spans
        .iter()
        .find(|span| span.id == "allocation")
        .unwrap();
    assert!(intervals
        .iter()
        .any(|interval| std::ptr::eq(interval.span, original)));
    assert_eq!(model.free_bytes("ram", 10), 0x180);
    // At an unknown checkpoint the static reservation remains visible, without
    // claiming that any allocator contents or free blocks were observed there.
    assert_eq!(model.physical_occupied_bytes(11)["sram"], 0x1000);
    assert_eq!(model.free_bytes("ram", 11), 0);
}

#[test]
fn observations_do_not_imply_interpolated_or_held_runtime() {
    let mut file = observation(vec![span(
        "allocation",
        0x1000,
        0x100,
        SpanKind::Allocation,
        None,
    )]);
    let mut second = file.snapshots[0].clone();
    second.time_ns = 20;
    second.spans.clear();
    file.snapshots.push(second);
    let model = MemoryModel::from_snapshot(file).unwrap();
    assert!(model.snapshot_at(10).is_some());
    assert!(model.snapshot_at(15).is_none());
    assert!(model.snapshot_at(21).is_none());
    assert_eq!(
        model
            .view("ram", AddressRange::new(0x1000, 0x1000).unwrap(), 15)
            .runtime,
        TimeConfidence::Unknown
    );
    assert_eq!(model.occupied_bytes("ram", 15), 0);
}

#[test]
fn malformed_snapshot_contract_is_rejected() {
    let valid = serde_json::to_value(observation(vec![])).unwrap();
    for bad in [
        {
            let mut value = valid.clone();
            value["schema_version"] = 2.into();
            value
        },
        {
            let mut value = valid.clone();
            value["build"]["elf_sha256"] = "not-a-hash".into();
            value
        },
        {
            let mut value = valid.clone();
            value["regions"][0]["start"] = "0x1000".into();
            value
        },
        {
            let mut value = valid.clone();
            value["regions"][0]["size"] = (-1).into();
            value
        },
        {
            let mut value = valid.clone();
            value["regions"][0]["size"] = 1.5.into();
            value
        },
        {
            let mut value = valid.clone();
            value["regions"][0]["size"] = 0.into();
            value
        },
        {
            let mut value = valid.clone();
            value["regions"][0]["start"] = u64::MAX.into();
            value
        },
    ] {
        assert!(parse_snapshot(&bad.to_string()).is_err(), "accepted {bad}");
    }
}

#[test]
fn duplicate_or_reversed_points_are_rejected() {
    let mut file = observation(vec![]);
    file.snapshots.push(file.snapshots[0].clone());
    assert!(MemoryModel::from_snapshot(file.clone()).is_err());
    file.snapshots[1].time_ns = 9;
    assert!(MemoryModel::from_snapshot(file).is_err());
}

#[test]
fn invalid_nesting_and_sibling_overlap_are_rejected() {
    for spans in [
        vec![
            span("a", 0x1000, 32, SpanKind::Allocation, None),
            span("b", 0x1010, 32, SpanKind::Allocation, None),
        ],
        vec![
            span("a", 0x1000, 32, SpanKind::Allocation, None),
            span("b", 0x1010, 32, SpanKind::Stack, Some("a")),
        ],
        vec![span("a", 0x1000, 32, SpanKind::Allocation, Some("missing"))],
        vec![
            span("a", 0x1000, 32, SpanKind::Allocation, Some("b")),
            span("b", 0x1000, 32, SpanKind::Allocation, Some("a")),
        ],
        vec![
            span("free", 0x1000, 32, SpanKind::Free, None),
            span("live", 0x1000, 16, SpanKind::Allocation, Some("free")),
        ],
        vec![span("outside", 0x1ff0, 32, SpanKind::Allocation, None)],
        vec![
            span("duplicate", 0x1000, 16, SpanKind::Allocation, None),
            span("duplicate", 0x1010, 16, SpanKind::Allocation, None),
        ],
    ] {
        assert!(MemoryModel::from_snapshot(observation(spans)).is_err());
    }
}

#[test]
fn completeness_counts_guard_chunks_not_stack_overlays() {
    let mut file = observation(vec![
        span("arena", 0x1000, 0x1000, SpanKind::HeapArena, None),
        span("guard", 0x1000, 16, SpanKind::Reserved, Some("arena")),
        span(
            "allocation",
            0x1010,
            0x1f0,
            SpanKind::Allocation,
            Some("arena"),
        ),
        span("stack", 0x1020, 0x100, SpanKind::Stack, Some("allocation")),
        span("free", 0x1200, 0xe00, SpanKind::Free, Some("arena")),
    ]);
    file.snapshots[0].complete_regions.push("ram".into());
    let model = MemoryModel::from_snapshot(file.clone()).unwrap();
    let stats = &model.heap_statistics("ram", 10)[0];
    assert_eq!(stats.reserved_bytes, 16);
    assert_eq!(stats.free_chunk_bytes, 0xe00);
    assert_eq!(stats.largest_free_chunk, 0xe00);
    assert_eq!(stats.unknown_bytes, 0);
    file.snapshots[0].spans.last_mut().unwrap().size -= 1;
    assert!(MemoryModel::from_snapshot(file).is_err());
}

fn aliased_file() -> SnapshotFile {
    let mut file = observation(vec![span("a", 0x1000, 256, SpanKind::Allocation, None)]);
    file.regions[0].physical = Some(PhysicalBacking {
        id: "sram".into(),
        offset: 0,
    });
    let mut alias = region("alias", 0x3000, 0x1000);
    alias.physical = Some(PhysicalBacking {
        id: "sram".into(),
        offset: 0,
    });
    file.regions.push(alias);
    file.physical_resources.push(PhysicalResource {
        id: "sram".into(),
        name: "SRAM".into(),
        size: 0x1000,
        banks: vec![],
    });
    let mut duplicate = span("alias-a", 0x3000, 256, SpanKind::Allocation, None);
    duplicate.region = "alias".into();
    file.snapshots[0].spans.push(duplicate);
    file
}

#[test]
fn aliases_share_backing_bytes_and_unmapped_windows_are_not_assumed_physical() {
    let model = MemoryModel::from_snapshot(aliased_file()).unwrap();
    assert_eq!(model.occupied_bytes("ram", 10), 256);
    assert_eq!(model.occupied_bytes("alias", 10), 256);
    assert_eq!(model.physical_occupied_bytes(10)["sram"], 256);
    let unmapped = MemoryModel::from_snapshot(observation(vec![span(
        "a",
        0x1000,
        256,
        SpanKind::Allocation,
        None,
    )]))
    .unwrap();
    assert!(unmapped.physical_occupied_bytes(10).is_empty());
}

#[test]
fn impossible_or_unbounded_alias_mappings_are_rejected() {
    let mut file = aliased_file();
    file.regions[1].physical.as_mut().unwrap().offset = 1;
    assert!(MemoryModel::from_snapshot(file).is_err());
    let mut file = aliased_file();
    file.physical_resources.clear();
    assert!(MemoryModel::from_snapshot(file).is_err());
    let mut file = aliased_file();
    file.regions[1].physical.as_mut().unwrap().offset = u64::MAX;
    assert!(MemoryModel::from_snapshot(file).is_err());
}

#[test]
fn contradictory_free_and_allocated_physical_aliases_are_rejected() {
    let mut file = aliased_file();
    file.snapshots[0].spans[1].kind = SpanKind::Free;
    assert!(MemoryModel::from_snapshot(file).is_err());
}

#[test]
fn static_heap_reservation_does_not_override_observed_free_chunks() {
    let mut model = MemoryModel::from_snapshot(observation(vec![
        span("arena", 0x1000, 0x1000, SpanKind::HeapArena, None),
        span(
            "allocated",
            0x1000,
            0x100,
            SpanKind::Allocation,
            Some("arena"),
        ),
        span("free", 0x1100, 0xf00, SpanKind::Free, Some("arena")),
    ]))
    .unwrap();
    model
        .static_spans
        .push(span("bss-storage", 0x1000, 0x1000, SpanKind::Bss, None));
    model.validate().unwrap();
    assert_eq!(model.occupied_bytes("ram", 10), 0x100);
    assert_eq!(model.free_bytes("ram", 10), 0xf00);
    assert_eq!(model.source_groups("ram", 10)[0].bytes, 0x100);
    // Away from a point, only the known ELF reservation remains. No runtime
    // allocation or free state is carried forward.
    assert_eq!(model.occupied_bytes("ram", 11), 0x1000);
    assert_eq!(model.free_bytes("ram", 11), 0);
}

#[test]
fn heap_observation_cannot_silently_erase_live_elf_code_or_data() {
    for kind in [SpanKind::Code, SpanKind::ReadOnly, SpanKind::Data] {
        let mut model = MemoryModel::from_snapshot(observation(vec![
            span("arena", 0x1000, 0x1000, SpanKind::HeapArena, None),
            span("free", 0x1000, 0x1000, SpanKind::Free, Some("arena")),
        ]))
        .unwrap();
        model
            .static_spans
            .push(span("resident", 0x1000, 256, kind, None));
        assert!(model.validate().is_err());
    }
}

#[test]
fn deepest_explicit_source_overrides_broad_parent_attribution() {
    let mut model = MemoryModel::from_snapshot(observation(vec![])).unwrap();
    let mut parent = span("parent", 0x1000, 256, SpanKind::Code, None);
    parent.source = Some("broad-parent.o".into());
    let mut child = span("child", 0x1000, 64, SpanKind::Code, Some("parent"));
    child.source = Some("precise-child.o".into());
    model.static_spans = vec![parent, child];
    model.validate().unwrap();
    let groups = model.source_groups("ram", 10);
    assert_eq!(
        groups
            .iter()
            .find(|group| group.source.as_deref() == Some("precise-child.o"))
            .unwrap()
            .bytes,
        64
    );
    assert_eq!(
        groups
            .iter()
            .find(|group| group.source.as_deref() == Some("broad-parent.o"))
            .unwrap()
            .bytes,
        192
    );
}

#[test]
fn nested_pool_capacity_is_reported_per_allocator() {
    let mut file = observation(vec![
        span("heap", 0x1000, 0x1000, SpanKind::HeapArena, None),
        span(
            "allocation",
            0x1000,
            0x200,
            SpanKind::Allocation,
            Some("heap"),
        ),
        span("pool", 0x1010, 0x1f0, SpanKind::Pool, Some("allocation")),
        span("pool-free", 0x1010, 0x1f0, SpanKind::Free, Some("pool")),
        span("heap-free", 0x1200, 0xe00, SpanKind::Free, Some("heap")),
    ]);
    file.regions[0].physical = Some(PhysicalBacking {
        id: "ram".into(),
        offset: 0,
    });
    file.physical_resources.push(PhysicalResource {
        id: "ram".into(),
        name: "RAM".into(),
        size: 0x1000,
        banks: vec![],
    });
    let model = MemoryModel::from_snapshot(file).unwrap();
    model.validate().unwrap();
    let stats = model.heap_statistics("ram", 10);
    assert_eq!(stats[0].allocated_chunk_bytes, 0x200);
    assert_eq!(stats[0].free_chunk_bytes, 0xe00);
    assert_eq!(stats[1].free_chunk_bytes, 0x1f0);
    assert_eq!(model.physical_occupied_bytes(10)["ram"], 16);
}

#[test]
fn source_partition_counts_section_parent_and_children_once() {
    let mut model = MemoryModel::from_snapshot(observation(vec![])).unwrap();
    let root = span("text", 0x1000, 0x100, SpanKind::Code, None);
    let mut first = span("a", 0x1000, 0x40, SpanKind::Code, Some("text"));
    first.source = Some("libfoo.a(foo.o)".into());
    let mut second = span("b", 0x1040, 0x60, SpanKind::Code, Some("text"));
    second.source = Some("libbar.a(bar.o)".into());
    model.static_spans = vec![root, first, second];
    model.validate().unwrap();
    let groups = model.source_groups("ram", 10);
    assert_eq!(groups.iter().map(|group| group.bytes).sum::<u64>(), 0x100);
    assert_eq!(
        groups
            .iter()
            .find(|group| group.source.is_none())
            .unwrap()
            .bytes,
        0x60
    );
}

#[test]
fn gnu_map_wrapping_and_discarded_sections_are_handled() {
    let map = "Discarded input sections\n .text.dead 0x0 0x20 dead.o\nLinker script and memory map\n.text\n  0x1000 0x40\n .text.foo\n  0x1000 0x10 libfoo.a(foo.o)\n .text.bar 0x1010 0x10 path/bar.o\n *fill* 0x1020 0x20\n";
    let contributions = parse_gnu_map(map).unwrap();
    assert_eq!(contributions.len(), 2);
    assert_eq!(contributions[0].source, "libfoo.a(foo.o)");
    assert_eq!(contributions[1].start, 0x1010);
    assert!(parse_gnu_map(".text 0xffffffffffffffff 0x10\n").is_err());
    assert!(parse_gnu_map(".text 0x10000000000000000 0x10\n").is_err());
    let clipped = parse_gnu_map(".text 0x1000 0x10\n .text.foo 0x100f 0x20 foo.o\n").unwrap();
    assert_eq!(clipped[0].size, 1);
    assert!(clipped[0].shared);
    let merged =
        parse_gnu_map(".rodata 0x1000 0x10\n .rodata.str1.1 0x1000 0x40 strings.o\n").unwrap();
    assert_eq!(merged[0].size, 0x10);
    assert!(merged[0].shared);
}

#[test]
fn synthetic_demo_remains_explicit_and_valid() {
    let model = demo();
    model.validate().unwrap();
    assert!(model.build.label.contains("SYNTHETIC"));
    assert_eq!(model.snapshots.len(), 3);
    assert!(model.snapshot_at(500_000_000).is_none());
    let stats = &model.heap_statistics("demo-ram", 2_000_000_000)[0];
    assert_eq!(stats.free_chunk_bytes, 3328);
    assert_eq!(stats.largest_free_chunk, 2816);
}
