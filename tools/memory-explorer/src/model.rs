use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug)]
pub struct Error(pub String);

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(error: std::io::Error) -> Self {
        Self(error.to_string())
    }
}

impl From<serde_json::Error> for Error {
    fn from(error: serde_json::Error) -> Self {
        Self(error.to_string())
    }
}

/// A nonempty, checked half-open interval. Integer geometry is kept until the UI
/// subtracts its viewport origin, avoiding loss of high address bits in floats.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AddressRange {
    pub start: u64,
    pub end: u64,
}

impl AddressRange {
    pub fn new(start: u64, size: u64) -> Result<Self> {
        if size == 0 {
            return Err(Error("zero-length memory interval".into()));
        }
        let end = start
            .checked_add(size)
            .ok_or_else(|| Error("memory interval overflows u64".into()))?;
        Ok(Self { start, end })
    }

    pub fn size(self) -> u64 {
        self.end - self.start
    }

    pub fn contains(self, other: Self) -> bool {
        self.start <= other.start && other.end <= self.end
    }

    pub fn overlaps(self, other: Self) -> bool {
        self.start < other.end && other.start < self.end
    }

    pub fn intersection(self, other: Self) -> Option<Self> {
        let start = self.start.max(other.start);
        let end = self.end.min(other.end);
        (start < end).then_some(Self { start, end })
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct BuildIdentity {
    /// SHA-256 of the complete final ELF file, not of the ELF's load segments.
    pub elf_sha256: String,
    #[serde(default)]
    pub label: String,
}

impl BuildIdentity {
    pub fn validate(&self) -> Result<()> {
        if self.elf_sha256.len() != 64 || !self.elf_sha256.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err(Error(
                "build.elf_sha256 must contain 64 hexadecimal digits".into(),
            ));
        }
        Ok(())
    }

    pub fn matches(&self, other: &Self) -> bool {
        self.elf_sha256.eq_ignore_ascii_case(&other.elf_sha256)
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum AddressDomain {
    /// An address visible to the target CPU. No physical uniqueness is implied.
    #[default]
    CpuVirtual,
    /// Byte offsets in a named image file, not a claim about flash programming.
    Image,
    /// Explicit physical-resource coordinates from a supplied board descriptor.
    Physical,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RegionKind {
    Flash,
    Ram,
    Unknown,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct PhysicalResource {
    pub id: String,
    pub name: String,
    /// Capacity in bytes. Backing offsets start at zero within this resource.
    pub size: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct PhysicalBacking {
    pub id: String,
    /// Resource-relative byte offset corresponding to Region.start.
    pub offset: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct Region {
    pub id: String,
    pub name: String,
    pub start: u64,
    pub size: u64,
    pub kind: RegionKind,
    #[serde(default)]
    pub domain: AddressDomain,
    #[serde(default)]
    pub physical: Option<PhysicalBacking>,
}

impl Region {
    pub fn range(&self) -> Result<AddressRange> {
        AddressRange::new(self.start, self.size)
    }

    pub fn end(&self) -> u64 {
        self.start.saturating_add(self.size)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum SpanKind {
    Code,
    ReadOnly,
    Data,
    Bss,
    HeapArena,
    Allocation,
    Free,
    Stack,
    StackUsed,
    Pool,
    Reserved,
    Unknown,
}

impl SpanKind {
    /// Reservation containers are not themselves live allocations. A heap with
    /// no observed chunks must not appear completely occupied (or free).
    pub fn is_occupied(self) -> bool {
        matches!(
            self,
            Self::Code
                | Self::ReadOnly
                | Self::Data
                | Self::Bss
                | Self::Allocation
                | Self::Stack
                | Self::StackUsed
                | Self::Reserved
        )
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Span {
    pub id: String,
    pub name: String,
    pub region: String,
    pub start: u64,
    pub size: u64,
    pub kind: SpanKind,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub parent: Option<String>,
    #[serde(default)]
    pub detail: Option<String>,
}

impl Span {
    pub fn range(&self) -> Result<AddressRange> {
        AddressRange::new(self.start, self.size)
    }

    pub fn end(&self) -> u64 {
        self.start.saturating_add(self.size)
    }
}

/// ELF symbols are labels, not ownership intervals. Aliased/overlapping symbols
/// remain visible without counting their bytes repeatedly.
#[derive(Clone, Debug)]
pub struct Symbol {
    pub name: String,
    pub region: String,
    pub start: u64,
    pub size: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Snapshot {
    pub time_ns: u64,
    pub label: String,
    pub spans: Vec<Span>,
    /// Regions whose heap-arena children completely tile the observed arena.
    /// Omit for incomplete captures; unobserved gaps remain unknown.
    #[serde(default)]
    pub complete_regions: Vec<String>,
}

/// Version 1 observation envelope. JSON addresses and sizes are unsigned integer
/// numbers (never floats or hex strings). Additional capture metadata is retained
/// as opaque JSON; it cannot make interval or build validation less strict.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SnapshotFile {
    pub schema_version: u32,
    pub build: BuildIdentity,
    pub regions: Vec<Region>,
    #[serde(default)]
    pub physical_resources: Vec<PhysicalResource>,
    pub snapshots: Vec<Snapshot>,
    #[serde(default)]
    pub warnings: Vec<String>,
    #[serde(default)]
    pub capture: Option<serde_json::Value>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct LayoutFile {
    pub schema_version: u32,
    pub regions: Vec<Region>,
    #[serde(default)]
    pub physical_resources: Vec<PhysicalResource>,
}

#[derive(Clone, Debug)]
pub struct MemoryModel {
    pub build: BuildIdentity,
    pub regions: Vec<Region>,
    pub physical_resources: Vec<PhysicalResource>,
    pub static_spans: Vec<Span>,
    pub symbols: Vec<Symbol>,
    pub snapshots: Vec<Snapshot>,
    pub warnings: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TimeConfidence {
    Observed,
    Unknown,
}

#[derive(Debug)]
pub struct View<'a> {
    pub spans: Vec<&'a Span>,
    pub runtime: TimeConfidence,
}

#[derive(Clone, Debug)]
pub struct SourceGroup {
    pub source: Option<String>,
    pub bytes: u64,
    pub spans: usize,
}

#[derive(Clone, Debug)]
pub struct PhysicalInterval<'a> {
    pub resource: &'a str,
    pub range: AddressRange,
    pub span: &'a Span,
}

#[derive(Clone, Debug)]
pub struct HeapStats {
    pub arena: String,
    pub arena_bytes: u64,
    pub allocated_chunk_bytes: u64,
    pub reserved_bytes: u64,
    pub free_chunk_bytes: u64,
    pub largest_free_chunk: u64,
    pub unknown_bytes: u64,
}

impl MemoryModel {
    pub fn from_snapshot(file: SnapshotFile) -> Result<Self> {
        validate_snapshot_file(&file)?;
        let mut warnings = file.warnings;
        warnings.push("Snapshot identity records the supplied host ELF hash; target-image identity is not independently proved.".into());
        warnings.push(
            "Runtime state is unknown between point snapshots. Unowned gaps are not free heap."
                .into(),
        );
        let model = Self {
            build: file.build,
            regions: file.regions,
            physical_resources: file.physical_resources,
            static_spans: Vec::new(),
            symbols: Vec::new(),
            snapshots: file.snapshots,
            warnings,
        };
        model.validate()?;
        Ok(model)
    }

    pub fn validate(&self) -> Result<()> {
        self.build.validate()?;
        validate_regions(&self.regions, &self.physical_resources)?;
        validate_spans(&self.static_spans, &self.regions)?;
        validate_snapshots(&self.snapshots, &self.regions)?;
        for snapshot in &self.snapshots {
            self.validate_cpu_states(snapshot.time_ns)?;
            self.validate_physical_states(snapshot.time_ns)?;
        }
        Ok(())
    }

    pub fn snapshot_at(&self, time_ns: u64) -> Option<&Snapshot> {
        self.snapshots
            .binary_search_by_key(&time_ns, |snapshot| snapshot.time_ns)
            .ok()
            .map(|index| &self.snapshots[index])
    }

    /// Query a single CPU/image window at an exact observation coordinate.
    /// There is deliberately no nearest-snapshot hold or interpolation.
    pub fn view(&self, region: &str, viewport: AddressRange, time_ns: u64) -> View<'_> {
        let snapshot = self.snapshot_at(time_ns);
        let spans = self
            .static_spans
            .iter()
            .chain(
                snapshot
                    .into_iter()
                    .flat_map(|snapshot| snapshot.spans.iter()),
            )
            .filter(|span| {
                span.region == region && span.range().is_ok_and(|r| r.overlaps(viewport))
            })
            .collect();
        View {
            spans,
            runtime: if snapshot.is_some() {
                TimeConfidence::Observed
            } else {
                TimeConfidence::Unknown
            },
        }
    }

    /// Static resident extents outside observed allocator arenas, plus observed
    /// runtime allocated/reserved/stack extents. These are not requested payload
    /// bytes. A statically reserved heap is replaced by its observed chunks;
    /// unobserved arena gaps count as neither live nor free. Nesting counts once.
    pub fn occupied_bytes(&self, region: &str, time_ns: u64) -> u64 {
        union_size(
            self.occupied_ranges(region, time_ns)
                .into_iter()
                .map(|(range, _)| range)
                .collect(),
        )
    }

    /// Only explicitly declared `free` intervals count as free bytes. When
    /// nested allocators exist, use heap_statistics for allocatable capacity in
    /// one arena; free storage from different arenas is not interchangeable.
    pub fn free_bytes(&self, region: &str, time_ns: u64) -> u64 {
        union_size(
            self.snapshot_at(time_ns)
                .into_iter()
                .flat_map(|snapshot| snapshot.spans.iter())
                .filter(|span| span.region == region && span.kind == SpanKind::Free)
                .filter_map(|span| span.range().ok())
                .collect(),
        )
    }

    pub fn observed_spans(&self, time_ns: u64) -> impl Iterator<Item = &Span> {
        self.static_spans.iter().chain(
            self.snapshot_at(time_ns)
                .into_iter()
                .flat_map(|snapshot| snapshot.spans.iter()),
        )
    }

    /// Project only declared mappings. Unmapped CPU windows are excluded, not
    /// silently interpreted as physically unique storage.
    pub fn physical_intervals(&self, time_ns: u64) -> Vec<PhysicalInterval<'_>> {
        self.observed_spans(time_ns)
            .filter_map(|span| {
                let region = self
                    .regions
                    .iter()
                    .find(|region| region.id == span.region)?;
                let backing = region.physical.as_ref()?;
                let start = span
                    .start
                    .checked_sub(region.start)?
                    .checked_add(backing.offset)?;
                let range = AddressRange::new(start, span.size).ok()?;
                Some(PhysicalInterval {
                    resource: &backing.id,
                    range,
                    span,
                })
            })
            .collect()
    }

    /// Occupied physical bytes, unioned within each resource. Two CPU aliases
    /// into the same bytes, and nested allocation/stack labels, count once.
    pub fn physical_occupied_bytes(&self, time_ns: u64) -> BTreeMap<String, u64> {
        let mut groups: BTreeMap<String, Vec<AddressRange>> = BTreeMap::new();
        for region in &self.regions {
            if let Some(backing) = &region.physical {
                for (range, _) in self.occupied_ranges(&region.id, time_ns) {
                    let start = range.start - region.start + backing.offset;
                    groups
                        .entry(backing.id.clone())
                        .or_default()
                        .push(AddressRange {
                            start,
                            end: start + range.size(),
                        });
                }
            }
        }
        groups
            .into_iter()
            .map(|(key, ranges)| (key, union_size(ranges)))
            .collect()
    }

    /// Allocator metrics are scoped to immediate children of each observed heap
    /// arena. Stack overlays and reservation parents do not inflate them.
    pub fn heap_statistics(&self, region: &str, time_ns: u64) -> Vec<HeapStats> {
        let Some(snapshot) = self.snapshot_at(time_ns) else {
            return Vec::new();
        };
        snapshot
            .spans
            .iter()
            .filter(|span| {
                span.region == region && matches!(span.kind, SpanKind::HeapArena | SpanKind::Pool)
            })
            .map(|arena| {
                let mut stats = HeapStats {
                    arena: arena.id.clone(),
                    arena_bytes: arena.size,
                    allocated_chunk_bytes: 0,
                    reserved_bytes: 0,
                    free_chunk_bytes: 0,
                    largest_free_chunk: 0,
                    unknown_bytes: arena.size,
                };
                for child in snapshot
                    .spans
                    .iter()
                    .filter(|span| span.parent.as_deref() == Some(&arena.id))
                {
                    match child.kind {
                        SpanKind::Allocation => stats.allocated_chunk_bytes += child.size,
                        SpanKind::Reserved => stats.reserved_bytes += child.size,
                        SpanKind::Free => {
                            stats.free_chunk_bytes += child.size;
                            stats.largest_free_chunk = stats.largest_free_chunk.max(child.size);
                        }
                        _ => continue,
                    }
                    stats.unknown_bytes -= child.size;
                }
                stats
            })
            .collect()
    }

    fn occupied_ranges(&self, region_id: &str, time_ns: u64) -> Vec<(AddressRange, &Span)> {
        let Some(region) = self.regions.iter().find(|region| region.id == region_id) else {
            return Vec::new();
        };
        let snapshot = self.snapshot_at(time_ns);
        let mut arenas = Vec::new();
        if let Some(snapshot) = snapshot {
            for arena in snapshot
                .spans
                .iter()
                .filter(|span| matches!(span.kind, SpanKind::HeapArena | SpanKind::Pool))
            {
                if arena.region == region_id {
                    if let Ok(range) = arena.range() {
                        arenas.push(range);
                    }
                } else if let (Some(target), Some(origin)) = (
                    region.physical.as_ref(),
                    self.regions
                        .iter()
                        .find(|r| r.id == arena.region)
                        .and_then(|r| r.physical.as_ref().map(|p| (r, p))),
                ) {
                    if target.id == origin.1.id {
                        let start = arena.start - origin.0.start + origin.1.offset;
                        let source = AddressRange {
                            start,
                            end: start + arena.size,
                        };
                        let target_range = AddressRange {
                            start: target.offset,
                            end: target.offset + region.size,
                        };
                        if let Some(overlap) = source.intersection(target_range) {
                            let start = region.start + overlap.start - target.offset;
                            arenas.push(AddressRange {
                                start,
                                end: start + overlap.size(),
                            });
                        }
                    }
                }
            }
        }
        let mut result = Vec::new();
        for span in self
            .static_spans
            .iter()
            .filter(|span| span.region == region_id && span.kind.is_occupied())
        {
            if let Ok(range) = span.range() {
                let mut remaining = vec![range];
                if matches!(span.kind, SpanKind::Bss | SpanKind::Reserved) {
                    for arena in &arenas {
                        remaining = remaining
                            .into_iter()
                            .flat_map(|range| subtract_range(range, *arena))
                            .collect();
                    }
                }
                result.extend(remaining.into_iter().map(|range| (range, span)));
            }
        }
        if let Some(snapshot) = snapshot {
            for span in snapshot
                .spans
                .iter()
                .filter(|span| span.region == region_id && span.kind.is_occupied())
            {
                let Ok(range) = span.range() else {
                    continue;
                };
                let mut remaining = vec![range];
                for arena in snapshot.spans.iter().filter(|arena| {
                    arena.region == region_id
                        && matches!(arena.kind, SpanKind::HeapArena | SpanKind::Pool)
                }) {
                    if is_ancestor(span, arena, &snapshot.spans) {
                        if let Ok(mask) = arena.range() {
                            remaining = remaining
                                .into_iter()
                                .flat_map(|range| subtract_range(range, mask))
                                .collect();
                        }
                    }
                }
                result.extend(remaining.into_iter().map(|range| (range, span)));
            }
        }
        result
    }

    fn validate_physical_states(&self, time_ns: u64) -> Result<()> {
        let mut occupied: BTreeMap<&str, Vec<AddressRange>> = BTreeMap::new();
        for region in &self.regions {
            if let Some(backing) = &region.physical {
                for (range, _) in self.occupied_ranges(&region.id, time_ns) {
                    let start = range.start - region.start + backing.offset;
                    occupied.entry(&backing.id).or_default().push(AddressRange {
                        start,
                        end: start + range.size(),
                    });
                }
            }
        }
        for interval in self
            .physical_intervals(time_ns)
            .into_iter()
            .filter(|i| i.span.kind == SpanKind::Free)
        {
            if occupied
                .get(interval.resource)
                .is_some_and(|ranges| ranges.iter().any(|range| range.overlaps(interval.range)))
            {
                return Err(Error(format!(
                    "physical resource {} has contradictory free/occupied observations at time {}",
                    interval.resource, time_ns
                )));
            }
        }
        Ok(())
    }

    fn validate_cpu_states(&self, time_ns: u64) -> Result<()> {
        let Some(snapshot) = self.snapshot_at(time_ns) else {
            return Ok(());
        };
        for region in &self.regions {
            let occupied = self.occupied_ranges(&region.id, time_ns);
            for free in snapshot
                .spans
                .iter()
                .filter(|span| span.region == region.id && span.kind == SpanKind::Free)
            {
                if occupied
                    .iter()
                    .any(|(range, _)| range.overlaps(free.range().expect("validated span")))
                {
                    return Err(Error(format!(
                        "region {} has contradictory free/live extents at time {}",
                        region.id, time_ns
                    )));
                }
            }
        }
        Ok(())
    }

    /// Source contribution per displayed region. Ownership comes from deepest
    /// source-labelled intervals; unlabeled bytes remain a separate group.
    /// ELF sections and their map children never add up twice.
    pub fn source_groups(&self, region: &str, time_ns: u64) -> Vec<SourceGroup> {
        let spans = self.occupied_ranges(region, time_ns);
        let mut boundaries = BTreeSet::new();
        for (range, _) in &spans {
            boundaries.insert(range.start);
            boundaries.insert(range.end);
        }
        let boundaries: Vec<_> = boundaries.into_iter().collect();
        let mut totals: BTreeMap<Option<String>, u64> = BTreeMap::new();
        for points in boundaries.windows(2) {
            let active: Vec<_> = spans
                .iter()
                .filter(|(range, _)| range.start <= points[0] && points[1] <= range.end)
                .collect();
            if active.is_empty() {
                continue;
            }
            let sources: BTreeSet<_> = active
                .iter()
                .filter(|(_, span)| {
                    !active.iter().any(|(_, child)| {
                        child.source.is_some()
                            && (is_ancestor(span, child, &self.static_spans)
                                || self.snapshot_at(time_ns).is_some_and(|snapshot| {
                                    is_ancestor(span, child, &snapshot.spans)
                                }))
                    })
                })
                .filter_map(|(_, span)| span.source.clone())
                .collect();
            let source = if sources.len() == 1 {
                sources.into_iter().next()
            } else {
                None
            };
            *totals.entry(source).or_default() += points[1] - points[0];
        }
        totals
            .into_iter()
            .map(|(source, bytes)| SourceGroup {
                spans: spans
                    .iter()
                    .filter(|(_, span)| span.source == source)
                    .count(),
                source,
                bytes,
            })
            .collect()
    }
}

fn subtract_range(range: AddressRange, occupied: AddressRange) -> Vec<AddressRange> {
    let Some(overlap) = range.intersection(occupied) else {
        return vec![range];
    };
    let mut remaining = Vec::new();
    if range.start < overlap.start {
        remaining.push(AddressRange {
            start: range.start,
            end: overlap.start,
        });
    }
    if overlap.end < range.end {
        remaining.push(AddressRange {
            start: overlap.end,
            end: range.end,
        });
    }
    remaining
}

fn is_ancestor(ancestor: &Span, child: &Span, spans: &[Span]) -> bool {
    let mut cursor = child;
    // Ingest validation rejects cycles. The bound also makes this read API safe
    // if a caller modifies the public model before revalidating it.
    for _ in 0..spans.len() {
        let Some(parent) = cursor.parent.as_deref() else {
            return false;
        };
        if parent == ancestor.id {
            return true;
        }
        let Some(next) = spans.iter().find(|span| span.id == parent) else {
            return false;
        };
        cursor = next;
    }
    false
}

pub fn parse_snapshot(text: &str) -> Result<SnapshotFile> {
    let file: SnapshotFile = serde_json::from_str(text)?;
    validate_snapshot_file(&file)?;
    Ok(file)
}

pub fn parse_layout(text: &str) -> Result<LayoutFile> {
    let file: LayoutFile = serde_json::from_str(text)?;
    if file.schema_version != 1 {
        return Err(Error(format!(
            "unsupported layout schema_version {}",
            file.schema_version
        )));
    }
    validate_regions(&file.regions, &file.physical_resources)?;
    Ok(file)
}

pub fn validate_snapshot_file(file: &SnapshotFile) -> Result<()> {
    if file.schema_version != 1 {
        return Err(Error(format!(
            "unsupported snapshot schema_version {}",
            file.schema_version
        )));
    }
    file.build.validate()?;
    validate_regions(&file.regions, &file.physical_resources)?;
    validate_snapshots(&file.snapshots, &file.regions)
}

pub(crate) fn validate_regions(regions: &[Region], resources: &[PhysicalResource]) -> Result<()> {
    let mut resource_map = BTreeMap::new();
    for resource in resources {
        if resource.id.is_empty()
            || resource.size == 0
            || resource_map.insert(&resource.id, resource).is_some()
        {
            return Err(Error(format!(
                "invalid or duplicate physical resource {}",
                resource.id
            )));
        }
    }
    let mut ids = BTreeSet::new();
    for region in regions {
        if region.id.is_empty() || !ids.insert(&region.id) {
            return Err(Error(format!("invalid or duplicate region {}", region.id)));
        }
        region.range()?;
        if let Some(backing) = &region.physical {
            let resource = resource_map.get(&backing.id).ok_or_else(|| {
                Error(format!(
                    "region {} references missing physical resource {}",
                    region.id, backing.id
                ))
            })?;
            if AddressRange::new(backing.offset, region.size)?.end > resource.size {
                return Err(Error(format!(
                    "region {} mapping exceeds physical resource {}",
                    region.id, backing.id
                )));
            }
        } else if region.domain == AddressDomain::Physical {
            return Err(Error(format!(
                "physical region {} requires an explicit resource mapping",
                region.id
            )));
        }
    }
    // A CPU address must resolve to one window. Different alias windows may map
    // into overlapping resource offsets; that is intentional and allowed.
    let mut sorted: Vec<_> = regions
        .iter()
        .filter(|r| r.domain == AddressDomain::CpuVirtual)
        .collect();
    sorted.sort_by_key(|r| r.start);
    for pair in sorted.windows(2) {
        if pair[0].end() > pair[1].start {
            return Err(Error(format!(
                "overlapping CPU regions {} and {}",
                pair[0].id, pair[1].id
            )));
        }
    }
    Ok(())
}

fn validate_snapshots(snapshots: &[Snapshot], regions: &[Region]) -> Result<()> {
    if snapshots
        .windows(2)
        .any(|pair| pair[0].time_ns >= pair[1].time_ns)
    {
        return Err(Error("snapshot times must be strictly increasing".into()));
    }
    for snapshot in snapshots {
        validate_spans(&snapshot.spans, regions)?;
        for id in &snapshot.complete_regions {
            let region = regions
                .iter()
                .find(|r| &r.id == id)
                .ok_or_else(|| Error(format!("complete_regions references missing region {id}")))?;
            let arenas: Vec<_> = snapshot
                .spans
                .iter()
                .filter(|s| &s.region == id && s.kind == SpanKind::HeapArena)
                .collect();
            if arenas.is_empty() {
                return Err(Error(format!(
                    "complete region {id} contains no heap arena"
                )));
            }
            for arena in arenas {
                let children: Vec<_> = snapshot
                    .spans
                    .iter()
                    .filter(|s| s.parent.as_deref() == Some(&arena.id))
                    .collect();
                if children.iter().any(|s| {
                    !matches!(
                        s.kind,
                        SpanKind::Free | SpanKind::Allocation | SpanKind::Reserved
                    )
                }) {
                    return Err(Error(format!(
                        "complete heap {} contains a non-chunk child",
                        arena.id
                    )));
                }
                let count = union_size(children.iter().filter_map(|s| s.range().ok()).collect());
                if count != arena.size {
                    return Err(Error(format!(
                        "complete heap {} has an unobserved gap",
                        arena.id
                    )));
                }
            }
            if region.domain != AddressDomain::CpuVirtual {
                return Err(Error(format!(
                    "heap completeness requires a CPU region: {id}"
                )));
            }
        }
    }
    Ok(())
}

pub(crate) fn validate_spans(spans: &[Span], regions: &[Region]) -> Result<()> {
    let mut ids = BTreeMap::new();
    for span in spans {
        if span.id.is_empty() || ids.insert(span.id.as_str(), span).is_some() {
            return Err(Error(format!("invalid or duplicate span {}", span.id)));
        }
        let region = regions
            .iter()
            .find(|r| r.id == span.region)
            .ok_or_else(|| {
                Error(format!(
                    "span {} references missing region {}",
                    span.id, span.region
                ))
            })?;
        if !region.range()?.contains(span.range()?) {
            return Err(Error(format!(
                "span {} exceeds region {}",
                span.id, region.id
            )));
        }
    }
    for span in spans {
        let mut seen = BTreeSet::from([span.id.as_str()]);
        let mut child = span;
        while let Some(parent_id) = child.parent.as_deref() {
            if !seen.insert(parent_id) {
                return Err(Error(format!("cycle in span parents at {}", span.id)));
            }
            let parent = ids.get(parent_id).ok_or_else(|| {
                Error(format!("span {} has missing parent {parent_id}", child.id))
            })?;
            if parent.region != child.region || !parent.range()?.contains(child.range()?) {
                return Err(Error(format!(
                    "span {} is outside parent {}",
                    child.id, parent.id
                )));
            }
            if parent.kind == SpanKind::Free {
                return Err(Error(format!(
                    "free span {} cannot own nested live spans",
                    parent.id
                )));
            }
            child = parent;
        }
    }
    let mut siblings: BTreeMap<(&str, Option<&str>), Vec<&Span>> = BTreeMap::new();
    for span in spans {
        siblings
            .entry((&span.region, span.parent.as_deref()))
            .or_default()
            .push(span);
    }
    for children in siblings.values_mut() {
        children.sort_by_key(|span| span.start);
        for pair in children.windows(2) {
            if pair[0].end() > pair[1].start {
                return Err(Error(format!("overlapping sibling spans {} and {} require an explicit containment relationship", pair[0].id, pair[1].id)));
            }
        }
    }
    Ok(())
}

pub(crate) fn union_size(mut ranges: Vec<AddressRange>) -> u64 {
    ranges.sort_by_key(|range| range.start);
    let mut total = 0;
    let mut end = 0;
    for range in ranges {
        total += range.end.saturating_sub(range.start.max(end));
        end = end.max(range.end);
    }
    total
}
