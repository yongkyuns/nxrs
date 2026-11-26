use crate::model::*;
use object::{Object, ObjectSection, ObjectSymbol};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

/// A GNU link-map input contribution. `source` is the object/archive spelling
/// present in the map, not a guessed original source file or marginal cost.
#[derive(Clone, Debug)]
pub struct MapContribution {
    pub output_section: String,
    pub input_section: String,
    pub start: u64,
    pub size: u64,
    pub source: String,
    /// Pre-merge input sizes or merged strings cannot establish exclusive ownership.
    pub shared: bool,
}

#[derive(Debug)]
struct ParsedMap {
    sections: BTreeMap<String, AddressRange>,
    contributions: Vec<MapContribution>,
}

#[derive(Debug)]
struct ElfSection {
    index: usize,
    name: String,
    range: AddressRange,
    file: Option<AddressRange>,
    kind: SpanKind,
    tls_template: bool,
}

/// Load a final ELF, optional GNU map and zero or one observation file.
pub fn load(elf: &Path, map: Option<&Path>, snapshot: Option<&Path>) -> Result<MemoryModel> {
    let snapshots: Vec<_> = snapshot.into_iter().map(Path::to_path_buf).collect();
    load_inputs(elf, map, &snapshots, None)
}

/// Merge files only after verifying the exact ELF identity. Snapshot inputs may
/// be supplied in any file order, but coordinates must be unique and increasing
/// inside each file. A supplied board layout takes precedence over smaller
/// snapshot windows only when it completely contains those windows.
pub fn load_inputs(
    elf: &Path,
    map: Option<&Path>,
    snapshots: &[PathBuf],
    layout: Option<&Path>,
) -> Result<MemoryModel> {
    let bytes = fs::read(elf).map_err(|error| Error(format!("{}: {error}", elf.display())))?;
    let file = object::File::parse(bytes.as_slice())
        .map_err(|error| Error(format!("ELF parse: {error}")))?;
    if file.format() == object::BinaryFormat::Elf && file.kind() == object::ObjectKind::Dynamic {
        return Err(Error("ET_DYN/PIE ELF requires a runtime load bias; relocation mapping is not supported by this milestone. Supply a final ET_EXEC firmware ELF.".into()));
    }
    if file.format() != object::BinaryFormat::Elf || file.kind() != object::ObjectKind::Executable {
        return Err(Error(
            "input must be a final ELF executable, not a relocatable object or archive".into(),
        ));
    }
    let build = BuildIdentity {
        elf_sha256: format!("{:x}", Sha256::digest(&bytes)),
        label: elf
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned(),
    };
    let mut model = MemoryModel {
        build,
        regions: Vec::new(),
        physical_resources: Vec::new(),
        static_spans: Vec::new(),
        symbols: Vec::new(),
        snapshots: Vec::new(),
        warnings: vec![
            "CPU addresses are virtual/address-window coordinates. Only declared backing mappings support physical accounting.".into(),
            "ELF file offsets describe this ELF container, not the programmed flash binary or its boot/image headers.".into(),
            "Runtime state is unknown between point snapshots. Unowned gaps are not free heap.".into(),
        ],
    };
    if let Some(path) = layout {
        let layout = parse_layout(&fs::read_to_string(path)?)?;
        model.regions = layout.regions;
        model.physical_resources = layout.physical_resources;
    }
    for path in snapshots {
        let mut snapshot_file = parse_snapshot(&fs::read_to_string(path)?)?;
        if !model.build.matches(&snapshot_file.build) {
            return Err(Error(format!(
                "snapshot {} belongs to a different ELF (SHA-256 mismatch)",
                path.display()
            )));
        }
        for resource in snapshot_file.physical_resources {
            if let Some(existing) = model
                .physical_resources
                .iter()
                .find(|r| r.id == resource.id)
            {
                if existing != &resource {
                    return Err(Error(format!(
                        "conflicting physical resource {}",
                        resource.id
                    )));
                }
            } else {
                model.physical_resources.push(resource);
            }
        }
        let mut remap = BTreeMap::new();
        for region in snapshot_file.regions {
            let id = merge_region(&mut model.regions, &region)?;
            remap.insert(region.id, id);
        }
        for snapshot in &mut snapshot_file.snapshots {
            for span in &mut snapshot.spans {
                span.region = remap.get(&span.region).expect("validated region").clone();
            }
            for id in &mut snapshot.complete_regions {
                *id = remap.get(id).expect("validated complete region").clone();
            }
            snapshot.complete_regions.sort();
            snapshot.complete_regions.dedup();
        }
        model.snapshots.extend(snapshot_file.snapshots);
        model.warnings.extend(snapshot_file.warnings);
    }
    model.snapshots.sort_by_key(|snapshot| snapshot.time_ns);
    if !model.snapshots.is_empty() {
        model.warnings.push("Snapshot host ELF hashes match this file; target-image identity and global capture quiescence are not independently proved.".into());
    }

    let mut sections = Vec::new();
    for section in file.sections() {
        let allocated = matches!(section.flags(), object::SectionFlags::Elf { sh_flags } if sh_flags & u64::from(object::elf::SHF_ALLOC) != 0);
        if !allocated || section.size() == 0 {
            continue;
        }
        let range = AddressRange::new(section.address(), section.size())?;
        let name = section.name().unwrap_or("<non-UTF8 section>").to_owned();
        let tls_template = matches!(section.flags(), object::SectionFlags::Elf { sh_flags } if sh_flags & u64::from(object::elf::SHF_TLS) != 0);
        let kind = match section.kind() {
            object::SectionKind::Text => SpanKind::Code,
            object::SectionKind::ReadOnlyData | object::SectionKind::ReadOnlyString => {
                SpanKind::ReadOnly
            }
            object::SectionKind::UninitializedData | object::SectionKind::UninitializedTls => {
                if name.contains("bss") {
                    SpanKind::Bss
                } else {
                    SpanKind::Reserved
                }
            }
            object::SectionKind::Data | object::SectionKind::Tls => SpanKind::Data,
            _ => SpanKind::Reserved,
        };
        let file_range = match section.file_range() {
            Some((start, size)) if size > 0 && kind != SpanKind::Bss => {
                let range = AddressRange::new(start, size)?;
                if range.end > bytes.len() as u64 {
                    return Err(Error(format!("section {name} exceeds ELF file bounds")));
                }
                Some(range)
            }
            _ => None,
        };
        sections.push(ElfSection {
            index: section.index().0,
            name,
            range,
            file: file_range,
            kind,
            tls_template,
        });
    }
    if sections.is_empty() {
        return Err(Error("final ELF has no allocated sections; section-header-free images are not supported by this milestone".into()));
    }
    if sections.iter().any(|section| section.tls_template) {
        model.warnings.push("ELF TLS sections are per-thread templates, not unique CPU RAM locations. Only their file-backed template bytes are shown; per-thread storage requires runtime observations.".into());
    }

    let parsed_map = if let Some(path) = map {
        let parsed = read_map(&fs::read_to_string(path)?)?;
        let mut matches = 0;
        for section in &sections {
            if let Some(mapped) = parsed.sections.get(&section.name) {
                if mapped != &section.range {
                    return Err(Error(format!(
                        "map output section {} does not match ELF address/size",
                        section.name
                    )));
                }
                matches += 1;
            }
        }
        if matches == 0 {
            return Err(Error(
                "GNU map has no matching allocated ELF output sections".into(),
            ));
        }
        model.warnings.push("GNU map address/size checks match ELF output sections; no cryptographic map identity is available. Sources identify retained input objects/archives. LTO, folded code and merged strings can prevent unique ownership; unmatched bytes remain unattributed.".into());
        Some(parsed)
    } else {
        model.warnings.push("No GNU map supplied: ELF symbols are available, but object/source ownership is unknown.".into());
        None
    };

    // Infer only the missing CPU address windows, not board RAM/flash capacity.
    let mut gaps = Vec::new();
    for section in sections.iter().filter(|section| !section.tls_template) {
        let mut remaining = vec![section.range];
        for region in model
            .regions
            .iter()
            .filter(|r| r.domain == AddressDomain::CpuVirtual)
        {
            remaining = remaining
                .into_iter()
                .flat_map(|range| subtract(range, region.range().expect("validated region")))
                .collect();
        }
        gaps.extend(remaining);
    }
    gaps.sort_by_key(|range| range.start);
    let mut merged: Vec<AddressRange> = Vec::new();
    for range in gaps {
        if let Some(last) = merged.last_mut().filter(|last| last.end >= range.start) {
            last.end = last.end.max(range.end);
        } else {
            merged.push(range);
        }
    }
    for (index, range) in merged.iter().enumerate() {
        let id = format!("elf-cpu-{index}");
        if model.regions.iter().any(|region| region.id == id) {
            return Err(Error(format!(
                "layout/snapshot uses reserved inferred region id {id}"
            )));
        }
        model.regions.push(Region {
            id,
            name: format!("ELF CPU window {:#x} (backing unknown)", range.start),
            start: range.start,
            size: range.size(),
            kind: RegionKind::Unknown,
            domain: AddressDomain::CpuVirtual,
            physical: None,
        });
    }
    if model.regions.iter().any(|region| region.id == "elf-file") {
        return Err(Error(
            "layout/snapshot uses reserved region id elf-file".into(),
        ));
    }
    model.regions.push(Region {
        id: "elf-file".into(),
        name: "ELF file offsets (not programmed flash layout)".into(),
        start: 0,
        size: bytes.len() as u64,
        kind: RegionKind::Unknown,
        domain: AddressDomain::Image,
        physical: None,
    });

    for section in &sections {
        for region in model
            .regions
            .iter()
            .filter(|r| r.domain == AddressDomain::CpuVirtual && !section.tls_template)
        {
            if let Some(range) = section.range.intersection(region.range()?) {
                let id = format!("elf-section-{}-{}", section.index, region.id);
                model.static_spans.push(Span {
                    id: id.clone(), name: section.name.clone(), region: region.id.clone(),
                    start: range.start, size: range.size(), kind: section.kind, source: None,
                    parent: None,
                    detail: Some(format!("Allocated ELF section; CPU VMA {:#x}, {} bytes. Storage backing is determined only by an explicit layout.", section.range.start, section.range.size())),
                });
                if let Some(map) = &parsed_map {
                    add_contributions(
                        &mut model.static_spans,
                        section,
                        range,
                        &id,
                        &region.id,
                        map,
                        0,
                    )?;
                }
            }
        }
        if let Some(range) = section.file {
            let id = format!("elf-file-section-{}", section.index);
            model.static_spans.push(Span {
                id: id.clone(), name: section.name.clone(), region: "elf-file".into(),
                start: range.start, size: range.size(), kind: section.kind, source: None,
                parent: None,
                detail: Some(format!("File-backed ELF section at file offset {:#x}; CPU VMA {:#x}. This is not a flash image load-address claim.", range.start, section.range.start)),
            });
            // ELF section file and runtime sizes agree for ordinary sections;
            // never force that relationship for a compressed/atypical section.
            if let Some(map) = &parsed_map {
                if range.size() == section.range.size() {
                    let delta = i128::from(range.start) - i128::from(section.range.start);
                    add_contributions(
                        &mut model.static_spans,
                        section,
                        section.range,
                        &id,
                        "elf-file",
                        map,
                        delta,
                    )?;
                }
            }
        }
    }
    for symbol in file.symbols() {
        if !symbol.is_definition()
            || symbol.is_undefined()
            || symbol.section_index().is_none()
            || symbol.kind() == object::SymbolKind::Tls
        {
            continue;
        }
        let Ok(name) = symbol.name() else {
            continue;
        };
        if name.is_empty() {
            continue;
        }
        if symbol.size() > 0 {
            AddressRange::new(symbol.address(), symbol.size())?;
        }
        if let Some(region) = model.regions.iter().find(|region| {
            region.domain == AddressDomain::CpuVirtual
                && region.start <= symbol.address()
                && symbol.address() < region.end()
        }) {
            model.symbols.push(Symbol {
                name: name.into(),
                region: region.id.clone(),
                start: symbol.address(),
                size: symbol.size(),
            });
        }
    }
    model.symbols.sort_by_key(|symbol| symbol.start);
    model
        .regions
        .sort_by_key(|region| (region.domain, region.start));
    model.warnings.sort();
    model.warnings.dedup();
    model.validate()?;
    Ok(model)
}

fn merge_region(regions: &mut Vec<Region>, incoming: &Region) -> Result<String> {
    if let Some(existing) = regions.iter().find(|region| region.id == incoming.id) {
        if existing != incoming {
            return Err(Error(format!(
                "conflicting region definition {}",
                incoming.id
            )));
        }
        return Ok(existing.id.clone());
    }
    if incoming.domain == AddressDomain::CpuVirtual {
        for existing in regions
            .iter()
            .filter(|region| region.domain == incoming.domain)
        {
            if existing.range()?.contains(incoming.range()?) {
                // A snapshot's narrower observed heap range can be placed in a
                // supplied RAM window. Explicit conflicting aliases cannot.
                if let Some(mapping) = &incoming.physical {
                    let consistent = existing.physical.as_ref().is_some_and(|other| {
                        other.id == mapping.id
                            && other.offset.checked_add(incoming.start - existing.start)
                                == Some(mapping.offset)
                    });
                    if !consistent {
                        return Err(Error(format!(
                            "conflicting backing mapping for {}",
                            incoming.id
                        )));
                    }
                }
                return Ok(existing.id.clone());
            }
            if existing.range()?.overlaps(incoming.range()?) {
                return Err(Error(format!(
                    "partially overlapping CPU windows {} and {}",
                    existing.id, incoming.id
                )));
            }
        }
    }
    regions.push(incoming.clone());
    Ok(incoming.id.clone())
}

fn subtract(range: AddressRange, occupied: AddressRange) -> Vec<AddressRange> {
    let Some(overlap) = range.intersection(occupied) else {
        return vec![range];
    };
    let mut result = Vec::new();
    if range.start < overlap.start {
        result.push(AddressRange {
            start: range.start,
            end: overlap.start,
        });
    }
    if overlap.end < range.end {
        result.push(AddressRange {
            start: overlap.end,
            end: range.end,
        });
    }
    result
}

fn add_contributions(
    spans: &mut Vec<Span>,
    section: &ElfSection,
    viewport: AddressRange,
    parent: &str,
    region: &str,
    map: &ParsedMap,
    delta: i128,
) -> Result<()> {
    let entries: Vec<_> = map
        .contributions
        .iter()
        .filter(|item| item.output_section == section.name)
        .filter_map(|item| {
            AddressRange::new(item.start, item.size)
                .ok()?
                .intersection(viewport)
                .map(|range| (item, range))
        })
        .collect();
    let mut boundaries = BTreeSet::new();
    for (_, range) in &entries {
        boundaries.insert(range.start);
        boundaries.insert(range.end);
    }
    let boundaries: Vec<_> = boundaries.into_iter().collect();
    for (index, points) in boundaries.windows(2).enumerate() {
        let active: Vec<_> = entries
            .iter()
            .filter(|(_, range)| range.start <= points[0] && points[1] <= range.end)
            .collect();
        if active.is_empty() {
            continue;
        }
        let sources: BTreeSet<_> = active.iter().map(|(item, _)| item.source.clone()).collect();
        let source = if sources.len() == 1 && !active.iter().any(|(item, _)| item.shared) {
            sources.into_iter().next()
        } else {
            None
        };
        let shifted = i128::from(points[0]) + delta;
        let start = u64::try_from(shifted)
            .map_err(|_| Error("map file-offset projection overflows".into()))?;
        spans.push(Span {
            id: format!("{parent}-source-{index}"),
            name: if active.len() == 1 && !active[0].0.shared { active[0].0.input_section.clone() } else { "Shared map input range".into() },
            region: region.into(), start, size: points[1] - points[0], kind: section.kind,
            source: source.clone(), parent: Some(parent.into()),
            detail: Some(match source {
                Some(owner) => format!("GNU map input object/archive: {owner}. Retained bytes; source-line and marginal ownership are not established."),
                None => "Merged or overlapping map contributions have ambiguous ownership; bytes count once.".into(),
            }),
        });
    }
    Ok(())
}

pub fn parse_gnu_map(text: &str) -> Result<Vec<MapContribution>> {
    Ok(read_map(text)?.contributions)
}

fn hex(text: &str) -> Option<u64> {
    u64::from_str_radix(
        text.strip_prefix("0x")
            .or_else(|| text.strip_prefix("0X"))?,
        16,
    )
    .ok()
}

fn read_map(text: &str) -> Result<ParsedMap> {
    let mut result = ParsedMap {
        sections: BTreeMap::new(),
        contributions: Vec::new(),
    };
    let body = text
        .split_once("Linker script and memory map")
        .map_or(text, |(_, body)| body);
    let mut current: Option<String> = None;
    let mut pending_output: Option<String> = None;
    let mut pending_input: Option<String> = None;
    for line in body.lines() {
        let fields: Vec<_> = line.split_whitespace().collect();
        if fields.is_empty() {
            continue;
        }
        for field in &fields {
            if let Some(digits) = field
                .strip_prefix("0x")
                .or_else(|| field.strip_prefix("0X"))
            {
                if !digits.is_empty()
                    && digits.bytes().all(|c| c.is_ascii_hexdigit())
                    && u64::from_str_radix(digits, 16).is_err()
                {
                    return Err(Error(format!("GNU map integer exceeds u64: {field}")));
                }
            }
        }
        if line.starts_with('.') {
            pending_input = None;
            let name = fields[0].to_owned();
            if fields.len() >= 3 {
                if let (Some(start), Some(size)) = (hex(fields[1]), hex(fields[2])) {
                    if size > 0 {
                        result
                            .sections
                            .insert(name.clone(), AddressRange::new(start, size)?);
                    }
                    current = Some(name);
                    pending_output = None;
                    continue;
                }
            }
            pending_output = Some(name);
            continue;
        }
        if let Some(name) = pending_output.take() {
            if fields.len() >= 2 {
                if let (Some(start), Some(size)) = (hex(fields[0]), hex(fields[1])) {
                    if size > 0 {
                        result
                            .sections
                            .insert(name.clone(), AddressRange::new(start, size)?);
                    }
                    current = Some(name);
                    continue;
                }
            }
            current = None;
        }
        if line.starts_with(char::is_whitespace) && fields[0].starts_with('.') {
            let name = fields[0].to_owned();
            if fields.len() >= 4 {
                if let (Some(start), Some(size)) = (hex(fields[1]), hex(fields[2])) {
                    push_map_item(
                        &mut result,
                        current.as_deref(),
                        name,
                        start,
                        size,
                        fields[3..].join(" "),
                    )?;
                    pending_input = None;
                    continue;
                }
            }
            pending_input = Some(name);
            continue;
        }
        if let Some(name) = pending_input.take() {
            if fields.len() >= 3 {
                if let (Some(start), Some(size)) = (hex(fields[0]), hex(fields[1])) {
                    push_map_item(
                        &mut result,
                        current.as_deref(),
                        name,
                        start,
                        size,
                        fields[2..].join(" "),
                    )?;
                }
            }
        }
    }
    Ok(result)
}

fn push_map_item(
    map: &mut ParsedMap,
    output: Option<&str>,
    input: String,
    start: u64,
    size: u64,
    source: String,
) -> Result<()> {
    if size == 0 {
        return Ok(());
    }
    let range = AddressRange::new(start, size)?;
    let Some(output) = output else {
        return Ok(());
    };
    let Some(parent) = map.sections.get(output) else {
        return Ok(());
    };
    // GNU ld can print pre-merge input sizes exceeding the final output range
    // (including .debug_str in normal linked executables). Keep only retained
    // bytes, with unknown ownership whenever clipping or merging is involved.
    let Some(retained) = parent.intersection(range) else {
        return Ok(());
    };
    let shared = retained != range || merged_input(&input);
    // Linker expressions, fill and wildcards do not establish an input owner.
    if source.starts_with('*') || source.starts_with("load address") {
        return Ok(());
    }
    map.contributions.push(MapContribution {
        output_section: output.into(),
        input_section: input,
        start: retained.start,
        size: retained.size(),
        source,
        shared,
    });
    Ok(())
}

fn merged_input(name: &str) -> bool {
    name.contains(".debug_str")
        || name.contains(".debug_line_str")
        || name.contains(".str1.")
        || name.contains(".str2.")
        || name.contains(".str4.")
        || name.contains(".str8.")
        || name.contains(".cst")
}
