use crate::*;

/// Explicitly synthetic observations for learning the viewer. These are not
/// target measurements, and the intervals between points are still unknown.
pub fn demo() -> MemoryModel {
    let regions = vec![
        Region {
            id: "demo-flash".into(),
            name: "Synthetic flash window".into(),
            start: 0x0800_0000,
            size: 8192,
            kind: RegionKind::Flash,
            domain: AddressDomain::CpuVirtual,
            physical: Some(PhysicalBacking {
                id: "flash".into(),
                offset: 0,
            }),
        },
        Region {
            id: "demo-ram".into(),
            name: "Synthetic SRAM".into(),
            start: 0x2000_0000,
            size: 8192,
            kind: RegionKind::Ram,
            domain: AddressDomain::CpuVirtual,
            physical: Some(PhysicalBacking {
                id: "sram".into(),
                offset: 0,
            }),
        },
        Region {
            id: "demo-ram-alias".into(),
            name: "Synthetic SRAM alias (same backing)".into(),
            start: 0x3000_0000,
            size: 8192,
            kind: RegionKind::Ram,
            domain: AddressDomain::CpuVirtual,
            physical: Some(PhysicalBacking {
                id: "sram".into(),
                offset: 0,
            }),
        },
    ];
    let static_spans = vec![
        span(
            "text",
            ".text",
            "demo-flash",
            0x0800_0000,
            1024,
            SpanKind::Code,
            None,
            None,
        ),
        span(
            "text-app",
            "main / synthetic application object",
            "demo-flash",
            0x0800_0000,
            640,
            SpanKind::Code,
            Some("app/demo/main.rs"),
            Some("text"),
        ),
        span(
            "text-service",
            "stream / synthetic service object",
            "demo-flash",
            0x0800_0280,
            384,
            SpanKind::Code,
            Some("service/stream/worker.rs"),
            Some("text"),
        ),
        span(
            "rodata",
            ".rodata",
            "demo-flash",
            0x0800_0400,
            512,
            SpanKind::ReadOnly,
            Some("service/stream/config.rs"),
            None,
        ),
        span(
            "data",
            ".data",
            "demo-ram",
            0x2000_0000,
            128,
            SpanKind::Data,
            Some("app/demo/config.rs"),
            None,
        ),
        span(
            "bss",
            ".bss",
            "demo-ram",
            0x2000_0080,
            256,
            SpanKind::Bss,
            None,
            None,
        ),
        span(
            "bss-queue",
            "Synthetic queue state",
            "demo-ram",
            0x2000_0080,
            128,
            SpanKind::Bss,
            Some("shared/messaging/queue.rs"),
            Some("bss"),
        ),
        span(
            "bss-state",
            "Synthetic application state",
            "demo-ram",
            0x2000_0100,
            64,
            SpanKind::Bss,
            Some("app/demo/state.rs"),
            Some("bss"),
        ),
        span(
            "heap-storage",
            "Static heap backing",
            "demo-ram",
            0x2000_0200,
            4096,
            SpanKind::Bss,
            None,
            None,
        ),
    ];
    let mut snapshots = Vec::new();
    for index in 0..3 {
        let mut spans = vec![
            span(
                "arena",
                "User heap",
                "demo-ram",
                0x2000_0200,
                4096,
                SpanKind::HeapArena,
                None,
                None,
            ),
            span(
                "stack",
                "stream worker stack reservation",
                "demo-ram",
                0x2000_1800,
                1024,
                SpanKind::Stack,
                Some("service/stream/worker.rs"),
                None,
            ),
            span(
                "stack-used",
                "Synthetic observed stack use",
                "demo-ram",
                0x2000_1a00,
                512,
                SpanKind::StackUsed,
                Some("service/stream/worker.rs"),
                Some("stack"),
            ),
        ];
        spans.push(span(
            "chunk-a",
            if index == 2 {
                "Released A"
            } else {
                "Allocation A (chunk extent)"
            },
            "demo-ram",
            0x2000_0200,
            512,
            if index == 2 {
                SpanKind::Free
            } else {
                SpanKind::Allocation
            },
            if index == 2 {
                None
            } else {
                Some("service/stream/frame.rs")
            },
            Some("arena"),
        ));
        if index == 0 {
            spans.push(span(
                "free-tail",
                "Free chunk",
                "demo-ram",
                0x2000_0400,
                3584,
                SpanKind::Free,
                None,
                Some("arena"),
            ));
        } else {
            spans.push(span(
                "chunk-b",
                "Allocation B (chunk extent)",
                "demo-ram",
                0x2000_0400,
                768,
                SpanKind::Allocation,
                Some("gateway/publisher/buffer.rs"),
                Some("arena"),
            ));
            spans.push(span(
                "free-tail",
                "Free chunk",
                "demo-ram",
                0x2000_0700,
                2816,
                SpanKind::Free,
                None,
                Some("arena"),
            ));
        }
        snapshots.push(Snapshot {
            time_ns: index * 1_000_000_000,
            label: format!(
                "Synthetic point {} — {}",
                index + 1,
                ["one allocation", "two allocations", "hole after A"][index as usize]
            ),
            spans,
            complete_regions: vec!["demo-ram".into()],
        });
    }
    MemoryModel {
        build: BuildIdentity { elf_sha256: "0".repeat(64), label: "SYNTHETIC DEMONSTRATION — no target measurement".into() },
        regions,
        physical_resources: vec![PhysicalResource { id: "flash".into(), name: "Synthetic flash".into(), size: 8192 }, PhysicalResource { id: "sram".into(), name: "Synthetic SRAM (aliases share these bytes)".into(), size: 8192 }],
        static_spans,
        symbols: vec![
            Symbol { name: "synthetic_main".into(), region: "demo-flash".into(), start: 0x0800_0020, size: 256 },
            Symbol { name: "synthetic_stream_worker".into(), region: "demo-flash".into(), start: 0x0800_0280, size: 320 },
            Symbol { name: "__synthetic_heap_start (zero-size marker)".into(), region: "demo-ram".into(), start: 0x2000_0200, size: 0 },
        ],
        snapshots,
        warnings: vec![
            "SYNTHETIC DEMONSTRATION: addresses, sources, allocations, stack use and physical mappings are invented examples.".into(),
            "The three points are observations only. Runtime state between them is unknown; this is not a reconstructed allocation trace.".into(),
            "Allocation extents include allocator overhead; requested payload sizes are not represented.".into(),
        ],
    }
}

#[allow(clippy::too_many_arguments)]
fn span(
    id: &str,
    name: &str,
    region: &str,
    start: u64,
    size: u64,
    kind: SpanKind,
    source: Option<&str>,
    parent: Option<&str>,
) -> Span {
    Span {
        id: id.into(),
        name: name.into(),
        region: region.into(),
        start,
        size,
        kind,
        source: source.map(str::to_owned),
        parent: parent.map(str::to_owned),
        detail: Some("Synthetic demonstration data".into()),
    }
}
