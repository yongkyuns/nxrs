use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use eframe::egui::{
    self, Align2, Color32, FontId, Pos2, Rect, RichText, Sense, Stroke, StrokeKind, Vec2,
};
use nxrs_memory_explorer::{AddressDomain, MemoryModel, RegionKind, Span, SpanKind};

const BACKGROUND: Color32 = Color32::from_rgb(13, 18, 27);
const PANEL: Color32 = Color32::from_rgb(19, 26, 37);
const SURFACE: Color32 = Color32::from_rgb(25, 34, 47);
const LINE: Color32 = Color32::from_rgb(43, 55, 72);
const TEXT: Color32 = Color32::from_rgb(224, 232, 242);
const MUTED: Color32 = Color32::from_rgb(139, 157, 177);
const ACCENT: Color32 = Color32::from_rgb(82, 215, 191);
const WARNING: Color32 = Color32::from_rgb(244, 193, 100);
const PALETTE: [Color32; 9] = [
    Color32::from_rgb(84, 194, 177),
    Color32::from_rgb(100, 161, 235),
    Color32::from_rgb(180, 137, 232),
    Color32::from_rgb(235, 172, 100),
    Color32::from_rgb(215, 127, 156),
    Color32::from_rgb(136, 189, 116),
    Color32::from_rgb(93, 190, 222),
    Color32::from_rgb(219, 140, 106),
    Color32::from_rgb(163, 170, 233),
];

#[derive(Clone, Default)]
pub struct InputPaths {
    pub elf: String,
    pub map: String,
    pub layout: String,
    pub snapshots: Vec<String>,
}

impl InputPaths {
    pub fn has_inputs(&self) -> bool {
        !self.elf.is_empty()
            || !self.map.is_empty()
            || !self.layout.is_empty()
            || !self.snapshots.is_empty()
    }

    fn load(&self) -> Result<MemoryModel, String> {
        if self.elf.trim().is_empty() {
            return Err("Choose the final ELF before loading captured memory.".into());
        }
        nxrs_memory_explorer::load_inputs(
            Path::new(self.elf.trim()),
            optional_path(&self.map),
            &self
                .snapshots
                .iter()
                .filter(|path| !path.trim().is_empty())
                .map(|path| PathBuf::from(path.trim()))
                .collect::<Vec<_>>(),
            optional_path(&self.layout),
        )
        .map_err(|error| error.to_string())
    }
}

fn optional_path(value: &str) -> Option<&Path> {
    (!value.trim().is_empty()).then(|| Path::new(value.trim()))
}

pub struct SmokeOptions {
    pub screenshot: PathBuf,
    pub result: Arc<Mutex<Option<Result<(), String>>>>,
}

struct SmokeState {
    options: SmokeOptions,
    frame: usize,
    screenshot_requested: bool,
    screenshot_saved: bool,
    checks: Vec<&'static str>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct AddressWindow {
    start: u64,
    end: u64,
}

impl AddressWindow {
    fn span(self) -> u64 {
        self.end.saturating_sub(self.start).max(1)
    }

    fn fraction(self, address: u64) -> f64 {
        // Subtract integer addresses first: adjacent addresses above 2^53 must
        // remain distinct when a small interval is magnified.
        if address >= self.start {
            address.saturating_sub(self.start) as f64 / self.span() as f64
        } else {
            -((self.start - address) as f64) / self.span() as f64
        }
    }

    fn at_fraction(self, fraction: f64) -> u64 {
        let offset = (fraction.clamp(0.0, 1.0) * self.span() as f64).round() as u64;
        self.start.saturating_add(offset).min(self.end)
    }

    fn fit_inside(self, bounds: Self) -> Self {
        let size = self.span().min(bounds.span());
        let start = self
            .start
            .max(bounds.start)
            .min(bounds.end.saturating_sub(size));
        Self {
            start,
            end: start.saturating_add(size),
        }
    }

    fn zoom(self, factor: f64, anchor: u64, bounds: Self) -> Self {
        let old_size = self.span();
        let new_size = (old_size as f64 * factor)
            .round()
            .clamp(1.0, bounds.span() as f64) as u64;
        let before = anchor.saturating_sub(self.start).min(old_size);
        let new_before = ((new_size as u128 * before as u128) / old_size as u128) as u64;
        let start = anchor
            .saturating_sub(new_before)
            .clamp(bounds.start, bounds.end - new_size);
        Self {
            start,
            end: start + new_size,
        }
    }

    fn pan(self, fraction: f64, bounds: Self) -> Self {
        let offset = (self.span() as f64 * fraction.abs()).round() as u64;
        let size = self.span().min(bounds.span());
        let start = if fraction >= 0.0 {
            self.start.saturating_add(offset)
        } else {
            self.start.saturating_sub(offset)
        }
        .clamp(bounds.start, bounds.end - size);
        Self {
            start,
            end: start + size,
        }
    }
}

#[derive(Clone)]
struct DisplayRegion {
    key: String,
    name: String,
    kind: String,
    bounds: AddressWindow,
    members: Vec<String>,
    physical: bool,
    domain: AddressDomain,
}

#[derive(Clone)]
struct DisplaySpan {
    span: Span,
    bounds: AddressWindow,
    symbol: bool,
    runtime: bool,
}

impl DisplaySpan {
    fn source(&self) -> &str {
        self.span.source.as_deref().unwrap_or("Unattributed")
    }

    fn matches(&self, filter: &str) -> bool {
        if filter.is_empty() {
            return true;
        }
        let filter = filter.to_lowercase();
        self.span.name.to_lowercase().contains(&filter)
            || self.source().to_lowercase().contains(&filter)
            || self.span.region.to_lowercase().contains(&filter)
            || format!("{:x}", self.span.start).contains(filter.trim_start_matches("0x"))
    }

    fn lane(&self) -> usize {
        if self.symbol {
            return 1;
        }
        if !self.runtime {
            return usize::from(self.span.parent.is_some());
        }
        match self.span.kind {
            SpanKind::Stack | SpanKind::StackUsed => 3,
            _ => 2,
        }
    }

    fn color(&self) -> Color32 {
        match self.span.kind {
            SpanKind::Free => Color32::from_rgb(48, 64, 79),
            SpanKind::Unknown => Color32::from_rgb(91, 79, 56),
            SpanKind::Reserved => Color32::from_rgb(102, 118, 139),
            _ => source_color(self.source()),
        }
    }
}

pub struct MemoryExplorer {
    inputs: InputPaths,
    snapshot_entry: String,
    model: Option<MemoryModel>,
    symbol_labels: Vec<Span>,
    synthetic: bool,
    physical: bool,
    region: String,
    viewport: AddressWindow,
    time_ns: Option<u64>,
    selected: Option<String>,
    filter: String,
    goto: String,
    show_inputs: bool,
    show_symbols: bool,
    error: Option<String>,
    status: String,
    smoke: Option<SmokeState>,
}

impl MemoryExplorer {
    pub fn new(
        inputs: InputPaths,
        demo: bool,
        smoke: Option<SmokeOptions>,
    ) -> Result<Self, String> {
        let model = if demo {
            Some(nxrs_memory_explorer::demo())
        } else if inputs.has_inputs() {
            Some(inputs.load()?)
        } else {
            None
        };
        let mut app = Self {
            inputs,
            snapshot_entry: String::new(),
            model: None,
            symbol_labels: Vec::new(),
            synthetic: demo,
            physical: false,
            region: String::new(),
            viewport: AddressWindow { start: 0, end: 1 },
            time_ns: None,
            selected: None,
            filter: String::new(),
            goto: String::new(),
            show_inputs: false,
            show_symbols: true,
            error: None,
            status: String::new(),
            smoke: smoke.map(|options| SmokeState {
                options,
                frame: 0,
                screenshot_requested: false,
                screenshot_saved: false,
                checks: Vec::new(),
            }),
        };
        if let Some(model) = model {
            app.install_model(model, demo);
        }
        Ok(app)
    }

    fn install_model(&mut self, model: MemoryModel, synthetic: bool) {
        self.symbol_labels = symbol_labels(&model);
        self.time_ns = model
            .snapshots
            .get(usize::from(model.snapshots.len() > 1))
            .map(|snapshot| snapshot.time_ns);
        self.model = Some(model);
        self.synthetic = synthetic;
        self.physical = false;
        self.selected = None;
        self.error = None;
        self.filter.clear();
        let regions = self.regions();
        if let Some(region) = regions
            .iter()
            .find(|region| region.kind.starts_with("RAM"))
            .or_else(|| regions.first())
        {
            self.select_region(region);
        }
        self.status = "Loaded. Click a block to inspect; scroll to zoom; drag to pan.".into();
    }

    fn regions(&self) -> Vec<DisplayRegion> {
        let Some(model) = &self.model else {
            return Vec::new();
        };
        if !self.physical {
            return model
                .regions
                .iter()
                .filter(|region| region.size > 0)
                .map(|region| DisplayRegion {
                    key: region.id.clone(),
                    name: region.name.clone(),
                    kind: format!(
                        "{} · {}",
                        region_kind(region.kind),
                        match region.domain {
                            AddressDomain::CpuVirtual => "CPU addresses",
                            AddressDomain::Image => "ELF file offsets",
                            AddressDomain::Physical => "physical coordinates",
                        }
                    ),
                    bounds: AddressWindow {
                        start: region.start,
                        end: region.start.saturating_add(region.size),
                    },
                    members: vec![region.id.clone()],
                    physical: false,
                    domain: region.domain,
                })
                .collect();
        }
        model
            .physical_resources
            .iter()
            .map(|resource| DisplayRegion {
                key: resource.id.clone(),
                name: resource.name.clone(),
                kind: "Declared backing offsets".into(),
                bounds: AddressWindow {
                    start: 0,
                    end: resource.size,
                },
                members: model
                    .regions
                    .iter()
                    .filter(|region| {
                        region
                            .physical
                            .as_ref()
                            .is_some_and(|backing| backing.id == resource.id)
                    })
                    .map(|region| region.id.clone())
                    .collect(),
                physical: true,
                domain: AddressDomain::Physical,
            })
            .collect()
    }

    fn current_region(&self) -> Option<DisplayRegion> {
        self.regions()
            .into_iter()
            .find(|region| region.key == self.region)
    }

    fn select_region(&mut self, region: &DisplayRegion) {
        self.region.clone_from(&region.key);
        self.viewport = region.bounds;
        self.selected = None;
    }

    fn spans(&self) -> Vec<DisplaySpan> {
        let (Some(model), Some(region)) = (&self.model, self.current_region()) else {
            return Vec::new();
        };
        let snapshot = self.time_ns.and_then(|time| {
            model
                .snapshots
                .iter()
                .find(|snapshot| snapshot.time_ns == time)
        });
        let spans = model
            .static_spans
            .iter()
            .map(|span| (span, false, false))
            .chain(
                snapshot
                    .into_iter()
                    .flat_map(|snapshot| snapshot.spans.iter().map(|span| (span, false, true))),
            )
            .chain(self.symbol_labels.iter().map(|span| (span, true, false)));
        spans
            .filter(|(span, symbol, _)| {
                region.members.contains(&span.region) && (span.size > 0 || *symbol)
            })
            .filter_map(|(span, symbol, runtime)| {
                let start = if region.physical {
                    let source_region = model.regions.iter().find(|r| r.id == span.region)?;
                    let backing = source_region.physical.as_ref()?;
                    backing
                        .offset
                        .checked_add(span.start.checked_sub(source_region.start)?)?
                } else {
                    span.start
                };
                Some(DisplaySpan {
                    span: span.clone(),
                    bounds: AddressWindow {
                        start,
                        end: start.checked_add(span.size)?,
                    },
                    symbol,
                    runtime,
                })
            })
            .collect()
    }

    fn exact_snapshot(&self) -> bool {
        self.model.as_ref().is_some_and(|model| {
            self.time_ns.is_some_and(|time| {
                model
                    .snapshots
                    .iter()
                    .any(|snapshot| snapshot.time_ns == time)
            })
        })
    }

    fn switch_address_mode(&mut self, physical: bool) {
        self.physical = physical;
        if let Some(region) = self.regions().first() {
            self.select_region(region);
        }
    }

    fn focus(&mut self, span: &DisplaySpan) {
        self.selected = Some(span.span.id.clone());
        if let Some(region) = self.current_region() {
            let margin = span.bounds.span() / 4;
            self.viewport = AddressWindow {
                start: span.bounds.start.saturating_sub(margin),
                end: span.bounds.end.saturating_add(margin),
            }
            .fit_inside(region.bounds);
        }
    }

    fn goto_address(&mut self) {
        match parse_address(&self.goto) {
            Ok(address) => {
                let regions = self.regions();
                let target = self
                    .current_region()
                    .filter(|region| address >= region.bounds.start && address < region.bounds.end)
                    .or_else(|| {
                        regions
                            .iter()
                            .find(|region| {
                                address >= region.bounds.start && address < region.bounds.end
                            })
                            .cloned()
                    });
                if let Some(region) = target {
                    if region.key != self.region {
                        self.select_region(&region);
                    }
                    let size = self.viewport.span().min(4096).min(region.bounds.span());
                    let start = address
                        .saturating_sub(size / 2)
                        .clamp(region.bounds.start, region.bounds.end - size);
                    self.viewport = AddressWindow {
                        start,
                        end: start + size,
                    };
                    self.selected = self
                        .spans()
                        .into_iter()
                        .filter(|span| {
                            address >= span.bounds.start
                                && (address < span.bounds.end
                                    || (span.span.size == 0 && address == span.bounds.start))
                        })
                        .min_by_key(|span| span.bounds.span())
                        .map(|span| span.span.id);
                    self.error = None;
                    self.status = format!("Centered on {address:#x}.");
                } else {
                    self.error = Some(format!(
                        "Address {address:#x} is outside the declared regions in this view."
                    ));
                }
            }
            Err(error) => self.error = Some(error),
        }
    }

    fn adjacent_snapshot(&mut self, next: bool) {
        let Some(model) = &self.model else {
            return;
        };
        let now = self.time_ns.unwrap_or(0);
        let target = if next {
            model
                .snapshots
                .iter()
                .find(|snapshot| snapshot.time_ns > now)
        } else {
            model
                .snapshots
                .iter()
                .rev()
                .find(|snapshot| snapshot.time_ns < now)
        };
        if let Some(target) = target {
            self.time_ns = Some(target.time_ns);
        }
    }

    fn header(&mut self, ctx: &egui::Context) {
        egui::TopBottomPanel::top("header")
            .frame(egui::Frame::new().fill(PANEL).inner_margin(egui::Margin::symmetric(20, 14)))
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.label(RichText::new("NXRS").color(ACCENT).strong().size(21.0));
                    ui.label(RichText::new("/  Memory Explorer").size(19.0));
                    ui.add_space(12.0);
                    if self.synthetic {
                        badge(ui, "SYNTHETIC DEMO", WARNING);
                    } else if self.model.is_some() {
                        badge(ui, "FIRMWARE ARTIFACTS", ACCENT);
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.button("Open files").clicked() {
                            self.show_inputs = true;
                        }
                        if self.model.is_some() {
                            let has_physical = self.model.as_ref().is_some_and(|model| !model.physical_resources.is_empty());
                            if ui
                                .add_enabled(has_physical, egui::Button::new("Physical backing").selected(self.physical))
                                .on_hover_text("Only mappings explicitly supplied by the layout/capture are shown. Coordinates are offsets in each backing store.")
                                .clicked()
                            {
                                self.switch_address_mode(true);
                            }
                            if ui.selectable_label(!self.physical, "Runtime / image").clicked() {
                                self.switch_address_mode(false);
                            }
                        }
                    });
                });
                if let Some(model) = &self.model {
                    ui.add_space(5.0);
                    ui.label(
                        RichText::new(format!(
                            "{}  ·  ELF {}",
                            model.build.label,
                            abbreviated(&model.build.elf_sha256, 16)
                        ))
                        .size(11.0)
                        .color(MUTED),
                    );
                }
            });
    }

    fn sidebar(&mut self, ctx: &egui::Context) {
        egui::SidePanel::left("regions")
            .default_width(246.0)
            .width_range(205.0..=360.0)
            .frame(egui::Frame::new().fill(PANEL).inner_margin(16))
            .show(ctx, |ui| {
                eyebrow(ui, if self.physical { "BACKING STORES" } else { "ADDRESS SPACES" });
                ui.add_space(10.0);
                egui::ScrollArea::vertical().id_salt("region-scroll").show(ui, |ui| {
                    for region in self.regions() {
                        let selected = region.key == self.region;
                        let frame = egui::Frame::new()
                            .fill(if selected { Color32::from_rgb(29, 55, 59) } else { SURFACE })
                            .stroke(Stroke::new(1.0, if selected { ACCENT } else { LINE }))
                            .corner_radius(7)
                            .inner_margin(11);
                        let response = frame
                            .show(ui, |ui| {
                                ui.set_min_width((ui.available_width() - 2.0).max(0.0));
                                ui.label(RichText::new(&region.name).strong().color(if selected { ACCENT } else { TEXT }));
                                ui.label(RichText::new(&region.kind).small().color(MUTED));
                                ui.label(RichText::new(format_bytes(region.bounds.span())).size(15.0));
                                ui.label(RichText::new(format!("{:#x} – {:#x}", region.bounds.start, region.bounds.end)).monospace().size(10.0).color(MUTED));
                                if region.members.len() > 1 {
                                    ui.label(RichText::new(format!("{} declared mapping windows", region.members.len())).small().color(WARNING));
                                }
                            })
                            .response;
                        if ui.interact(response.rect, ui.id().with(&region.key), Sense::click()).clicked() {
                            self.select_region(&region);
                        }
                        ui.add_space(7.0);
                    }
                    if self.physical {
                        ui.label(RichText::new("Unmapped regions remain available in Runtime / image. Aliases share backing coordinates; they are not additional RAM.").small().color(MUTED));
                    }
                    let owners: Vec<_> = self.spans().into_iter().filter(|span| !span.symbol && matches!(span.span.kind, SpanKind::HeapArena | SpanKind::Pool | SpanKind::Stack)).collect();
                    if !owners.is_empty() {
                        ui.add_space(10.0);
                        egui::CollapsingHeader::new("Captured arenas and stacks").default_open(true).show(ui, |ui| {
                            for owner in owners {
                                if ui.selectable_label(self.selected.as_ref() == Some(&owner.span.id), format!("{} · {}", owner.span.name, format_bytes(owner.span.size))).clicked() {
                                    self.focus(&owner);
                                }
                            }
                        });
                    }
                    ui.add_space(15.0);
                    eyebrow(ui, "FIND SOURCE OR SYMBOL");
                    ui.add_space(7.0);
                    ui.add(egui::TextEdit::singleline(&mut self.filter).hint_text("e.g. imu, .bss, sample").desired_width(f32::INFINITY));
                    if !self.filter.is_empty() {
                        let results: Vec<_> = self.spans().into_iter().filter(|span| span.matches(&self.filter)).collect();
                        ui.label(RichText::new(format!("{} matches in this region", results.len())).small().color(MUTED));
                        for result in results.iter().take(12) {
                            if ui.selectable_label(self.selected.as_ref() == Some(&result.span.id), &result.span.name).clicked() {
                                self.focus(result);
                            }
                        }
                        ui.label(RichText::new("Other blocks dim; their addresses stay fixed.").small().color(MUTED));
                        if ui.small_button("Clear filter").clicked() {
                            self.filter.clear();
                        }
                    }
                    ui.add_space(18.0);
                    eyebrow(ui, "EVIDENCE");
                    ui.add_space(7.0);
                    ui.label(RichText::new(if self.synthetic {
                        "Demonstration addresses and captures. No board was measured."
                    } else {
                        "Static placement comes from the linked image. Runtime blocks appear only at captured checkpoints."
                    }).small().color(if self.synthetic { WARNING } else { MUTED }));
                    if let Some(model) = &self.model {
                        for warning in &model.warnings {
                            ui.add_space(5.0);
                            ui.label(RichText::new(warning).small().color(WARNING));
                        }
                    }
                });
            });
    }

    fn inspector(&mut self, ctx: &egui::Context) {
        egui::SidePanel::right("inspector")
            .default_width(278.0)
            .width_range(230.0..=420.0)
            .frame(egui::Frame::new().fill(PANEL).inner_margin(18))
            .show(ctx, |ui| {
                egui::ScrollArea::vertical().id_salt("inspector-scroll").show(ui, |ui| {
                eyebrow(ui, "BLOCK INSPECTOR");
                ui.add_space(15.0);
                let selected = self.selected.as_ref().and_then(|id| {
                    self.spans().into_iter().find(|span| &span.span.id == id)
                });
                if let Some(block) = selected {
                    let color = block.color();
                    badge(ui, if block.symbol { "ELF SYMBOL · LABEL" } else { kind_name(block.span.kind) }, color);
                    ui.add_space(10.0);
                    ui.heading(&block.span.name);
                    ui.add_space(18.0);
                    field(ui, "SIZE", &format!("{}  ({} bytes)", format_bytes(block.span.size), block.span.size));
                    let domain = self.current_region().map(|region| region.domain).unwrap_or(AddressDomain::CpuVirtual);
                    field(ui, if self.physical { "BACKING OFFSET" } else if domain == AddressDomain::Image { "ELF FILE OFFSET" } else { "START ADDRESS" }, &format!("{:#018x}", block.bounds.start));
                    field(ui, "END · EXCLUSIVE", &format!("{:#018x}", block.bounds.end));
                    field(ui, "REGION", &block.span.region);
                    if self.physical {
                        field(ui, "ORIGINAL ADDRESS", &format!("{:#018x}", block.span.start));
                    }
                    field(ui, "SOURCE", block.source());
                    if let Some(parent) = &block.span.parent {
                        field(ui, "CONTAINING OBJECT", parent);
                    }
                    let heap = self.model.as_ref().zip(self.time_ns).and_then(|(model, time)| model.heap_statistics(&block.span.region, time).into_iter().find(|stats| stats.arena == block.span.id || block.span.parent.as_ref() == Some(&stats.arena)));
                    if let Some(heap) = heap {
                        egui::CollapsingHeader::new("Arena observations").default_open(true).show(ui, |ui| {
                            egui::Grid::new("heap-statistics").num_columns(2).show(ui, |ui| {
                                for (label, bytes) in [("Allocated chunks", heap.allocated_chunk_bytes), ("Recorded free", heap.free_chunk_bytes), ("Largest free chunk", heap.largest_free_chunk), ("Reserved", heap.reserved_bytes), ("Unobserved", heap.unknown_bytes)] {
                                    ui.label(RichText::new(label).small().color(MUTED));
                                    ui.label(RichText::new(format_bytes(bytes)).small());
                                    ui.end_row();
                                }
                            });
                            ui.label(RichText::new("Chunk extents include allocator overhead. Largest free is not an allocation-success guarantee.").small().color(MUTED));
                        });
                    }
                    if let Some(detail) = &block.span.detail {
                        ui.separator();
                        ui.label(RichText::new(detail).small().color(MUTED));
                    }
                    if matches!(block.span.kind, SpanKind::Stack | SpanKind::StackUsed) {
                        ui.add_space(9.0);
                        ui.label(RichText::new("Stack reservations can overlap heap allocations. These layers are not additive. A sampled/high-water extent does not reconstruct call-frame history.").small().color(WARNING));
                    }
                    ui.add_space(15.0);
                    if ui.button("Zoom to block").clicked() {
                        self.focus(&block);
                    }
                    if ui.button(if self.physical { "Copy backing offset" } else { "Copy address" }).clicked() {
                        ctx.copy_text(format!("{:#x}", block.bounds.start));
                    }
                } else {
                    ui.label(RichText::new("Select a memory block").size(18.0));
                    ui.add_space(8.0);
                    ui.label(RichText::new("Inspect its exact range, source attribution, containing object, and capture evidence.").color(MUTED));
                    ui.add_space(24.0);
                    field(ui, "NAVIGATE", "Scroll to zoom around the pointer. Drag to pan. Double-click a block to fit it.");
                    field(ui, "KEYBOARD", "F / Home: fit region\n+ / −: zoom\n← / →: pan\nPage Up / Down: captured checkpoint");
                }
                ui.add_space(24.0);
                ui.separator();
                ui.add_space(12.0);
                eyebrow(ui, "LAYER KEY");
                ui.add_space(8.0);
                ui.label(RichText::new("Sections → linked static placement\nObjects → linker source contributions\nSymbols → ELF labels, possibly aliased\nHeap → captured allocator state\nStacks → task-owned overlays").small().color(MUTED));
                ui.add_space(8.0);
                ui.label(RichText::new("Empty or dim space is not automatically free memory. Free blocks require an explicit capture.").small().color(WARNING));
                });
            });
    }

    fn timeline(&mut self, ctx: &egui::Context) {
        egui::TopBottomPanel::bottom("timeline")
            .resizable(false)
            .exact_height(156.0)
            .frame(egui::Frame::new().fill(PANEL).inner_margin(egui::Margin::symmetric(20, 12)))
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    eyebrow(ui, "SNAPSHOT HISTORY");
                    ui.add_space(14.0);
                    if self.exact_snapshot() {
                        badge(ui, if self.synthetic { "SYNTHETIC CHECKPOINT" } else { "CAPTURED CHECKPOINT" }, ACCENT);
                    } else {
                        badge(ui, "RUNTIME STATE UNKNOWN", WARNING);
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.button("Next  ›").clicked() {
                            self.adjacent_snapshot(true);
                        }
                        if ui.button("‹  Previous").clicked() {
                            self.adjacent_snapshot(false);
                        }
                    });
                });
                let Some(model) = &self.model else { return; };
                if model.snapshots.is_empty() {
                    ui.add_space(15.0);
                    ui.label("No runtime snapshots loaded.");
                    ui.label(RichText::new("The static ELF view remains available. Load a matching capture to inspect heaps and task stacks.").color(MUTED));
                    return;
                }
                let snapshots: Vec<_> = model.snapshots.iter().map(|snapshot| (snapshot.time_ns, snapshot.label.clone())).collect();
                let first = snapshots.first().unwrap().0;
                let last = snapshots.last().unwrap().0;
                let range = AddressWindow { start: first, end: last.max(first.saturating_add(1)) };
                let (rect, response) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 63.0), Sense::click_and_drag());
                let line_rect = rect.shrink2(Vec2::new(18.0, 0.0));
                let y = rect.top() + 24.0;
                let painter = ui.painter();
                let mut x = line_rect.left();
                while x < line_rect.right() {
                    painter.line_segment([Pos2::new(x, y), Pos2::new((x + 5.0).min(line_rect.right()), y)], Stroke::new(1.0, LINE));
                    x += 10.0;
                }
                let x_at = |time| line_rect.left() + range.fraction(time) as f32 * line_rect.width();
                if let Some(pointer) = response.interact_pointer_pos().filter(|_| response.clicked() || response.dragged()) {
                    let nearest = snapshots.iter().min_by(|a,b| (x_at(a.0)-pointer.x).abs().total_cmp(&(x_at(b.0)-pointer.x).abs()));
                    self.time_ns = nearest.filter(|snapshot| (x_at(snapshot.0)-pointer.x).abs() < 10.0).map(|snapshot| snapshot.0)
                        .or_else(|| Some(range.at_fraction(((pointer.x-line_rect.left())/line_rect.width()) as f64)));
                }
                for (index, (time, label)) in snapshots.iter().enumerate() {
                    let x = x_at(*time);
                    let selected = self.time_ns == Some(*time);
                    painter.circle_filled(Pos2::new(x,y), if selected { 6.0 } else { 4.0 }, if selected { ACCENT } else { MUTED });
                    if selected {
                        painter.circle_stroke(Pos2::new(x,y), 10.0, Stroke::new(1.0, ACCENT));
                    }
                    let align = if index == 0 { Align2::LEFT_TOP } else if index+1 == snapshots.len() { Align2::RIGHT_TOP } else { Align2::CENTER_TOP };
                    let stride = snapshots.len().div_ceil((line_rect.width() / 240.0).max(1.0) as usize);
                    if selected || index == 0 || index + 1 == snapshots.len() || index % stride == 0 {
                        painter.text(Pos2::new(x, y+15.0), align, format!("{} · {}", format_time(time.saturating_sub(first)), abbreviated(label, 34)), FontId::proportional(10.0), if selected { TEXT } else { MUTED });
                    }
                }
                if let Some(time) = self.time_ns {
                    let x = x_at(time);
                    painter.line_segment([Pos2::new(x, rect.top()+3.0), Pos2::new(x,y+10.0)], Stroke::new(1.0, if self.exact_snapshot() { ACCENT } else { WARNING }));
                }
                ui.horizontal_wrapped(|ui| {
                    let exact = self.time_ns.and_then(|time| snapshots.iter().find(|snapshot| snapshot.0 == time));
                    if let Some((time, label)) = exact {
                        ui.label(RichText::new(format!("{label}  ·  supplied coordinate {time} ns")).color(TEXT));
                    } else {
                        ui.label(RichText::new("Between checkpoints: runtime blocks are hidden; no state is interpolated.").color(WARNING));
                    }
                    ui.label(RichText::new("Click a point to inspect. Click between points to see the evidence gap.").small().color(MUTED));
                });
            });
    }

    fn center(&mut self, ctx: &egui::Context) {
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(BACKGROUND).inner_margin(20))
            .show(ctx, |ui| {
                let available_height = ui.available_height();
                egui::ScrollArea::vertical().id_salt("canvas-scroll").auto_shrink([false, false]).show(ui, |ui| {
                let content_start = ui.cursor().top();
                let Some(region) = self.current_region() else {
                    ui.label("No addressable regions were declared in the input.");
                    return;
                };
                ui.horizontal(|ui| {
                    ui.heading(&region.name);
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.button("Fit region").clicked() {
                            self.viewport = region.bounds;
                        }
                        if ui.button("+").clicked() {
                            self.viewport = self.viewport.zoom(
                                0.5,
                                self.viewport.at_fraction(0.5),
                                region.bounds,
                            );
                        }
                        if ui.button("−").clicked() {
                            self.viewport = self.viewport.zoom(
                                2.0,
                                self.viewport.at_fraction(0.5),
                                region.bounds,
                            );
                        }
                    });
                });
                ui.label(RichText::new(if region.physical { "physical backing offsets" } else { match region.domain { AddressDomain::Image => "ELF file offsets · these are file bytes, not a flashed image", AddressDomain::CpuVirtual => "CPU address coordinates", AddressDomain::Physical => "declared physical coordinates" } }).small().color(MUTED));
                ui.add_space(12.0);
                self.metrics(ui);
                ui.add_space(12.0);
                ui.horizontal(|ui| {
                    let response = ui.add(
                        egui::TextEdit::singleline(&mut self.goto)
                            .hint_text("Go to hex address, e.g. 0x20001000")
                            .font(egui::TextStyle::Monospace)
                            .desired_width(270.0),
                    );
                    if ui.button("Go").clicked()
                        || (response.lost_focus()
                            && ui.input(|input| input.key_pressed(egui::Key::Enter)))
                    {
                        self.goto_address();
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.checkbox(&mut self.show_symbols, "ELF symbols");
                    });
                });
                ui.add_space(9.0);
                if let Some(error) = &self.error {
                    ui.label(RichText::new(error).color(Color32::LIGHT_RED));
                }
                let height = (available_height - (ui.cursor().top() - content_start) - 78.0).max(285.0);
                self.memory_canvas(ui, height);
                ui.add_space(12.0);
                self.legend(ui);
                ui.add_space(5.0);
                ui.label(RichText::new(&self.status).small().color(MUTED));
                });
            });
    }

    fn metrics(&self, ui: &mut egui::Ui) {
        let spans = self.spans();
        let allocations: Vec<_> = spans
            .iter()
            .filter(|span| span.span.kind == SpanKind::Allocation)
            .collect();
        let largest_free = spans
            .iter()
            .filter(|span| span.span.kind == SpanKind::Free)
            .map(|span| span.span.size)
            .max();
        let columns = [
            (
                "VISIBLE ADDRESS RANGE",
                format_bytes(self.viewport.span()),
                format!("{:#x} → {:#x}", self.viewport.start, self.viewport.end),
            ),
            (
                "ALLOCATION RECORDS",
                if self.exact_snapshot() {
                    allocations.len().to_string()
                } else {
                    "—".into()
                },
                if self.exact_snapshot() {
                    "At the selected checkpoint".into()
                } else {
                    "Runtime state not captured".into()
                },
            ),
            (
                "LARGEST OBSERVED FREE CHUNK",
                largest_free.map(format_bytes).unwrap_or_else(|| "—".into()),
                "Selected region · recorded extent".into(),
            ),
        ];
        ui.columns(3, |uis| {
            for (ui, (label, value, detail)) in uis.iter_mut().zip(columns) {
                egui::Frame::new()
                    .fill(SURFACE)
                    .corner_radius(7)
                    .inner_margin(11)
                    .show(ui, |ui| {
                        ui.set_min_width(ui.available_width());
                        ui.label(RichText::new(label).size(9.0).color(MUTED));
                        ui.label(RichText::new(value).size(22.0).strong());
                        ui.label(RichText::new(detail).size(9.0).color(MUTED));
                    });
            }
        });
    }

    fn memory_canvas(&mut self, ui: &mut egui::Ui, height: f32) {
        let Some(region) = self.current_region() else {
            return;
        };
        let (rect, response) = ui.allocate_exact_size(
            Vec2::new(ui.available_width(), height),
            Sense::click_and_drag(),
        );
        let plot = Rect::from_min_max(
            rect.min + Vec2::new(12.0, 35.0),
            rect.max - Vec2::new(12.0, 18.0),
        );
        if response.hovered() {
            let scroll = ui.input_mut(|input| std::mem::take(&mut input.smooth_scroll_delta));
            if let Some(pointer) = response.hover_pos().filter(|_| scroll.y != 0.0) {
                let anchor = self
                    .viewport
                    .at_fraction(((pointer.x - plot.left()) / plot.width()) as f64);
                self.viewport =
                    self.viewport
                        .zoom((-scroll.y as f64 * 0.004).exp(), anchor, region.bounds);
            }
            if scroll.x != 0.0 {
                self.viewport = self
                    .viewport
                    .pan(-scroll.x as f64 / plot.width() as f64, region.bounds);
            }
        }
        if response.dragged() {
            let dx = ui.input(|input| input.pointer.delta().x);
            self.viewport = self
                .viewport
                .pan(-dx as f64 / plot.width() as f64, region.bounds);
        }
        let painter = ui.painter().with_clip_rect(rect);
        painter.rect_filled(rect, 8, PANEL);
        painter.rect_stroke(rect, 8, Stroke::new(1.0, LINE), StrokeKind::Inside);
        let ticks = ((plot.width() / 125.0) as u64)
            .max(1)
            .min(self.viewport.span());
        for index in 0..=ticks {
            let address = self.viewport.start
                + ((self.viewport.span() as u128 * index as u128) / ticks as u128) as u64;
            let x = plot.left() + self.viewport.fraction(address) as f32 * plot.width();
            painter.line_segment(
                [Pos2::new(x, plot.top()), Pos2::new(x, plot.bottom())],
                Stroke::new(1.0, LINE.linear_multiply(0.6)),
            );
            let align = if index == 0 {
                Align2::LEFT_CENTER
            } else if index == ticks {
                Align2::RIGHT_CENTER
            } else {
                Align2::CENTER_CENTER
            };
            painter.text(
                Pos2::new(x, rect.top() + 18.0),
                align,
                format!("{address:#x}"),
                FontId::monospace(10.0),
                MUTED,
            );
        }
        let lanes: Vec<_> = if region.domain == AddressDomain::Image {
            vec![0, 1]
        } else {
            vec![0, 1, 2, 3]
        };
        let lane_height = plot.height() / lanes.len() as f32;
        let all_spans = self.spans();
        let labels = [
            "LINKED SECTIONS",
            "SOURCE OBJECTS / ELF LABELS",
            "HEAP / POOLS",
            "TASK STACKS",
        ];
        let mut hit: Option<DisplaySpan> = None;
        for (row, lane) in lanes.iter().enumerate() {
            let top = plot.top() + row as f32 * lane_height;
            painter.text(
                Pos2::new(plot.left() + 1.0, top + 5.0),
                Align2::LEFT_TOP,
                labels[*lane],
                FontId::proportional(10.0),
                MUTED,
            );
            let band = Rect::from_min_max(
                Pos2::new(plot.left(), top + 25.0),
                Pos2::new(plot.right(), top + lane_height - 12.0),
            );
            painter.rect_filled(band, 3, Color32::from_rgb(15, 21, 31));
            if *lane >= 2 && !self.exact_snapshot() {
                hatch(&painter, band, LINE);
                painter.text(
                    band.center(),
                    Align2::CENTER_CENTER,
                    "Runtime state unknown at this time",
                    FontId::proportional(12.0),
                    WARNING,
                );
                continue;
            }
            let mut spans: Vec<_> = all_spans
                .iter()
                .filter(|span| span.lane() == *lane && (!span.symbol || self.show_symbols))
                .collect();
            // Containers first, detail/usage overlays after them. Preserve the
            // address axis: filtering only changes intensity.
            spans.sort_by_key(|span| {
                (
                    span.symbol,
                    paint_priority(span.span.kind),
                    std::cmp::Reverse(span.span.size),
                )
            });
            let split_symbols =
                spans.iter().any(|span| span.symbol) && spans.iter().any(|span| !span.symbol);
            if spans.is_empty() {
                painter.text(
                    band.center(),
                    Align2::CENTER_CENTER,
                    if *lane >= 2 {
                        "No records in this capture"
                    } else {
                        "No linked objects in this layer"
                    },
                    FontId::proportional(11.0),
                    MUTED,
                );
            }
            for span in spans {
                if (span.bounds.end <= self.viewport.start && span.span.size != 0)
                    || span.bounds.start >= self.viewport.end
                    || (span.span.size == 0 && span.bounds.start < self.viewport.start)
                {
                    continue;
                }
                let left = plot.left()
                    + self
                        .viewport
                        .fraction(span.bounds.start.max(self.viewport.start))
                        as f32
                        * plot.width();
                let right = plot.left()
                    + self
                        .viewport
                        .fraction(span.bounds.end.min(self.viewport.end))
                        as f32
                        * plot.width();
                let mut extent_band = band;
                if split_symbols {
                    if span.symbol {
                        extent_band.min.y = band.center().y + 1.0;
                    } else {
                        extent_band.max.y = band.center().y - 1.0;
                    }
                }
                let inner = if span.span.kind == SpanKind::StackUsed && !span.symbol {
                    10.0
                } else {
                    3.0
                };
                let block_rect = Rect::from_min_max(
                    Pos2::new(left, extent_band.top() + inner),
                    Pos2::new(right, extent_band.bottom() - inner),
                );
                let selected = self.selected.as_ref() == Some(&span.span.id);
                let matched = span.matches(&self.filter);
                let mut color = span.color();
                if matches!(
                    span.span.kind,
                    SpanKind::HeapArena | SpanKind::Pool | SpanKind::Stack
                ) {
                    color = color.linear_multiply(0.32);
                }
                if !matched {
                    color = color.linear_multiply(0.16);
                }
                // Tiny intervals keep their true width. Zero-size ELF symbols
                // are ticks, never invented occupied bytes.
                if span.symbol && span.span.size == 0 {
                    painter.line_segment(
                        [block_rect.left_top(), block_rect.left_bottom()],
                        Stroke::new(1.0, if selected { Color32::WHITE } else { color }),
                    );
                } else {
                    painter.rect_filled(
                        block_rect,
                        2,
                        if span.symbol {
                            color.linear_multiply(0.35)
                        } else {
                            color
                        },
                    );
                    painter.rect_stroke(
                        block_rect,
                        2,
                        Stroke::new(
                            if selected { 2.0 } else { 0.7 },
                            if selected {
                                Color32::WHITE
                            } else {
                                color.gamma_multiply(1.2)
                            },
                        ),
                        StrokeKind::Inside,
                    );
                }
                if span.span.kind == SpanKind::Unknown && !span.symbol {
                    hatch(&painter, block_rect, WARNING.linear_multiply(0.45));
                }
                if block_rect.width() > 56.0 && matched {
                    let text = if block_rect.width() > 140.0 {
                        format!("{}  ·  {}", span.span.name, format_bytes(span.span.size))
                    } else {
                        span.span.name.clone()
                    };
                    let clipped = painter.with_clip_rect(block_rect.shrink(4.0));
                    clipped.text(
                        Pos2::new(block_rect.left() + 6.0, block_rect.center().y),
                        Align2::LEFT_CENTER,
                        text,
                        FontId::proportional(11.0),
                        if matches!(
                            span.span.kind,
                            SpanKind::HeapArena
                                | SpanKind::Stack
                                | SpanKind::Pool
                                | SpanKind::Free
                                | SpanKind::Reserved
                        ) {
                            TEXT
                        } else {
                            Color32::from_rgb(13, 22, 31)
                        },
                    );
                }
                if response.hover_pos().is_some_and(|pointer| {
                    block_rect
                        .expand2(Vec2::new(
                            if block_rect.width() < 2.0 { 1.0 } else { 0.0 },
                            0.0,
                        ))
                        .contains(pointer)
                }) {
                    hit = Some(span.clone());
                }
            }
        }
        if let Some(block) = hit {
            response.clone().on_hover_ui_at_pointer(|ui| {
                ui.label(RichText::new(&block.span.name).strong());
                ui.monospace(format!(
                    "{:#x}..{:#x} · {} bytes",
                    block.bounds.start, block.bounds.end, block.span.size
                ));
                ui.label(block.source());
                ui.label(
                    RichText::new(kind_name(block.span.kind))
                        .small()
                        .color(MUTED),
                );
            });
            if response.clicked() {
                self.selected = Some(block.span.id.clone());
            }
            if response.double_clicked() {
                self.focus(&block);
            }
        }
    }

    fn legend(&self, ui: &mut egui::Ui) {
        let sources: BTreeSet<_> = self
            .spans()
            .into_iter()
            .filter(|span| {
                !matches!(
                    span.span.kind,
                    SpanKind::Free | SpanKind::Unknown | SpanKind::Reserved
                )
            })
            .map(|span| span.source().to_owned())
            .collect();
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new("SOURCE COLORS").size(9.0).color(MUTED));
            for source in sources.iter().take(8) {
                let (rect, _) = ui.allocate_exact_size(Vec2::splat(8.0), Sense::hover());
                ui.painter().rect_filled(rect, 2, source_color(source));
                ui.label(RichText::new(short_source(source)).size(10.0).color(MUTED))
                    .on_hover_text(source);
                ui.add_space(4.0);
            }
            if sources.len() > 8 {
                ui.label(
                    RichText::new(format!("+{} sources", sources.len() - 8))
                        .small()
                        .color(MUTED),
                );
            }
        });
    }

    fn welcome(&mut self, ctx: &egui::Context) {
        egui::CentralPanel::default().frame(egui::Frame::new().fill(BACKGROUND).inner_margin(36)).show(ctx,|ui| {
            ui.add_space(52.0);
            ui.label(RichText::new("Inspect the memory your firmware uses.").size(30.0).strong());
            ui.add_space(12.0);
            ui.label(RichText::new("Explore linked sections, source ownership, heap blocks and task stacks at captured points in time.").size(16.0).color(MUTED));
            ui.add_space(30.0);
            egui::Frame::new().fill(PANEL).stroke(Stroke::new(1.0,LINE)).corner_radius(10).inner_margin(24).show(ui,|ui| {
                ui.set_max_width(750.0);
                self.input_fields(ui);
            });
            ui.add_space(22.0);
            if ui.button("Explore synthetic demonstration").clicked() {
                self.install_model(nxrs_memory_explorer::demo(),true);
            }
            ui.label(RichText::new("Demonstration data is explicitly synthetic and contains no device measurements.").small().color(MUTED));
        });
    }

    fn input_fields(&mut self, ui: &mut egui::Ui) {
        eyebrow(ui, "BUILD ARTIFACTS AND CAPTURES");
        ui.add_space(12.0);
        path_field(ui, "Final ELF", &mut self.inputs.elf, "/path/to/nuttx");
        path_field(
            ui,
            "Linker map · optional",
            &mut self.inputs.map,
            "/path/to/nuttx.map",
        );
        path_field(
            ui,
            "Region layout · optional",
            &mut self.inputs.layout,
            "/path/to/layout.json",
        );
        ui.add_space(7.0);
        ui.label(RichText::new("Runtime snapshots").small().color(MUTED));
        let mut remove = None;
        for (index, path) in self.inputs.snapshots.iter().enumerate() {
            ui.horizontal(|ui| {
                ui.label(RichText::new(path).monospace().size(11.0));
                if ui.small_button("Remove").clicked() {
                    remove = Some(index);
                }
            });
        }
        if let Some(index) = remove {
            self.inputs.snapshots.remove(index);
        }
        ui.horizontal(|ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.snapshot_entry)
                    .hint_text("/path/to/capture.json")
                    .desired_width(530.0),
            );
            if ui.button("Add").clicked() && !self.snapshot_entry.trim().is_empty() {
                self.inputs
                    .snapshots
                    .push(std::mem::take(&mut self.snapshot_entry));
            }
        });
        ui.add_space(10.0);
        ui.label(RichText::new("Drop ELF, .map and JSON files into this window, or paste paths above. Snapshot build identity is checked against the final ELF.").small().color(MUTED));
        ui.add_space(12.0);
        if ui
            .button(RichText::new("Load memory evidence").strong())
            .clicked()
        {
            if !self.snapshot_entry.trim().is_empty() {
                self.inputs
                    .snapshots
                    .push(std::mem::take(&mut self.snapshot_entry));
            }
            match self.inputs.load() {
                Ok(model) => {
                    self.install_model(model, false);
                    self.show_inputs = false;
                }
                Err(error) => self.error = Some(error),
            }
        }
        if let Some(error) = &self.error {
            ui.label(RichText::new(error).color(Color32::LIGHT_RED));
        }
    }

    fn file_window(&mut self, ctx: &egui::Context) {
        if !self.show_inputs {
            return;
        }
        let mut open = true;
        egui::Window::new("Open build and captures")
            .open(&mut open)
            .default_width(760.0)
            .resizable(true)
            .show(ctx, |ui| self.input_fields(ui));
        self.show_inputs &= open;
    }

    fn dropped_files(&mut self, ctx: &egui::Context) {
        let files = ctx.input(|input| input.raw.dropped_files.clone());
        if files.is_empty() {
            return;
        }
        for file in files {
            let Some(path) = file.path else {
                continue;
            };
            let value = path.to_string_lossy().into_owned();
            match path.extension().and_then(|ext| ext.to_str()) {
                Some("map") => self.inputs.map = value,
                Some("json") => {
                    let is_layout = std::fs::read(&path)
                        .ok()
                        .and_then(|bytes| serde_json::from_slice::<serde_json::Value>(&bytes).ok())
                        .is_some_and(|json| {
                            json.get("regions").is_some() && json.get("snapshots").is_none()
                        });
                    if is_layout {
                        self.inputs.layout = value;
                    } else if !self.inputs.snapshots.contains(&value) {
                        self.inputs.snapshots.push(value);
                    }
                }
                _ => self.inputs.elf = value,
            }
        }
        self.show_inputs = true;
    }

    fn keyboard(&mut self, ctx: &egui::Context) {
        if ctx.wants_keyboard_input() {
            return;
        }
        let Some(region) = self.current_region() else {
            return;
        };
        ctx.input(|input| {
            if input.key_pressed(egui::Key::F) || input.key_pressed(egui::Key::Home) {
                self.viewport = region.bounds;
            }
            if input.key_pressed(egui::Key::Plus) || input.key_pressed(egui::Key::Equals) {
                self.viewport =
                    self.viewport
                        .zoom(0.5, self.viewport.at_fraction(0.5), region.bounds);
            }
            if input.key_pressed(egui::Key::Minus) {
                self.viewport =
                    self.viewport
                        .zoom(2.0, self.viewport.at_fraction(0.5), region.bounds);
            }
            if input.key_pressed(egui::Key::ArrowLeft) {
                self.viewport = self.viewport.pan(-0.25, region.bounds);
            }
            if input.key_pressed(egui::Key::ArrowRight) {
                self.viewport = self.viewport.pan(0.25, region.bounds);
            }
            if input.key_pressed(egui::Key::PageDown) {
                self.adjacent_snapshot(true);
            }
            if input.key_pressed(egui::Key::PageUp) {
                self.adjacent_snapshot(false);
            }
        });
    }

    fn smoke_step(&mut self, ctx: &egui::Context) {
        let Some(mut smoke) = self.smoke.take() else {
            return;
        };
        smoke.frame += 1;
        let check = |condition: bool, message: &str| -> Result<(), String> {
            if condition {
                Ok(())
            } else {
                Err(message.into())
            }
        };
        let outcome = (|| -> Result<(), String> {
            if smoke.frame == 3 {
                let region = self.current_region().ok_or("No region rendered")?;
                let before = self.viewport;
                self.viewport = self
                    .viewport
                    .zoom(0.5, before.at_fraction(0.5), region.bounds);
                check(
                    self.viewport.span() <= before.span(),
                    "Zoom did not reduce address range",
                )?;
                self.viewport = self.viewport.pan(0.1, region.bounds);
                check(
                    self.viewport.start >= region.bounds.start
                        && self.viewport.end <= region.bounds.end,
                    "Pan escaped region bounds",
                )?;
                self.viewport = region.bounds;
                smoke.checks.push("integer address zoom/pan/fit");
                if let Some(span) = self
                    .spans()
                    .into_iter()
                    .find(|span| span.span.kind == SpanKind::Allocation)
                {
                    self.goto = format!("{:#x}", span.span.start);
                    self.goto_address();
                    check(self.error.is_none(), "Hex address lookup failed")?;
                    self.selected = Some(span.span.id.clone());
                    self.filter = span.source().to_owned();
                    check(
                        self.spans().iter().any(|span| span.matches(&self.filter)),
                        "Source search failed",
                    )?;
                    self.filter.clear();
                    smoke
                        .checks
                        .push("address lookup/source filtering/selection");
                }
                self.viewport = region.bounds;
            }
            if smoke.frame == 6 {
                if let Some(model) = self
                    .model
                    .as_ref()
                    .filter(|model| model.snapshots.len() > 1)
                {
                    let first = model.snapshots[0].time_ns;
                    let second = model.snapshots[1].time_ns;
                    if second > first + 1 {
                        self.time_ns = Some(first + (second - first) / 2);
                        check(
                            !self.exact_snapshot(),
                            "Inter-snapshot time was marked captured",
                        )?;
                        check(
                            !self
                                .spans()
                                .iter()
                                .any(|span| span.span.kind == SpanKind::Allocation),
                            "Runtime allocations leaked into unknown interval",
                        )?;
                        smoke
                            .checks
                            .push("unknown interval hides uncaptured allocations");
                    }
                    self.time_ns = Some(second);
                }
                if self.model.as_ref().is_some_and(|model| {
                    model.regions.iter().any(|region| region.physical.is_some())
                }) {
                    let old = self.region.clone();
                    self.switch_address_mode(true);
                    check(
                        !self.regions().is_empty(),
                        "Declared physical mapping disappeared",
                    )?;
                    self.switch_address_mode(false);
                    if let Some(region) = self.regions().iter().find(|region| region.key == old) {
                        self.select_region(region);
                    }
                    smoke.checks.push("declared backing mapping");
                }
                if let Some(span) = self
                    .spans()
                    .into_iter()
                    .find(|span| span.span.kind == SpanKind::Allocation)
                {
                    self.selected = Some(span.span.id);
                }
            }
            if smoke.frame >= 10 && !smoke.screenshot_requested {
                ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::UserData::default()));
                smoke.screenshot_requested = true;
            }
            let screenshot = ctx.input(|input| {
                input.events.iter().find_map(|event| match event {
                    egui::Event::Screenshot { image, .. } => Some(Arc::clone(image)),
                    _ => None,
                })
            });
            if let Some(image) = screenshot {
                check(
                    image.width() >= 800 && image.height() >= 500,
                    "Rendered screenshot is unexpectedly small",
                )?;
                save_ppm(&smoke.options.screenshot, &image)?;
                smoke.screenshot_saved = true;
                smoke.checks.push("actual egui/glow framebuffer captured");
                println!(
                    "Memory Explorer smoke passed: {}. Screenshot: {}",
                    smoke.checks.join("; "),
                    smoke.options.screenshot.display()
                );
                *smoke
                    .options
                    .result
                    .lock()
                    .map_err(|_| "Smoke result lock poisoned")? = Some(Ok(()));
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            }
            if smoke.frame > 180 && !smoke.screenshot_saved {
                return Err("Timed out waiting for the rendered screenshot".into());
            }
            Ok(())
        })();
        if let Err(error) = outcome {
            eprintln!("Memory Explorer smoke failed: {error}");
            if let Ok(mut result) = smoke.options.result.lock() {
                *result = Some(Err(error));
            }
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
        self.smoke = Some(smoke);
        ctx.request_repaint();
    }
}

impl eframe::App for MemoryExplorer {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.dropped_files(ctx);
        self.keyboard(ctx);
        self.header(ctx);
        if self.model.is_some() {
            self.timeline(ctx);
            self.sidebar(ctx);
            self.inspector(ctx);
            self.center(ctx);
        } else {
            self.welcome(ctx);
        }
        self.file_window(ctx);
        self.smoke_step(ctx);
    }
}

pub fn configure_style(ctx: &egui::Context) {
    let mut visuals = egui::Visuals::dark();
    visuals.panel_fill = PANEL;
    visuals.window_fill = PANEL;
    visuals.override_text_color = Some(TEXT);
    visuals.faint_bg_color = SURFACE;
    visuals.extreme_bg_color = BACKGROUND;
    visuals.selection.bg_fill = Color32::from_rgb(40, 103, 100);
    visuals.selection.stroke = Stroke::new(1.0, ACCENT);
    visuals.widgets.inactive.bg_fill = SURFACE;
    visuals.widgets.inactive.weak_bg_fill = SURFACE;
    visuals.widgets.hovered.bg_fill = Color32::from_rgb(44, 64, 78);
    visuals.widgets.active.bg_fill = Color32::from_rgb(35, 96, 90);
    ctx.set_visuals(visuals);
    let mut style = (*ctx.style()).clone();
    style.spacing.item_spacing = Vec2::new(8.0, 7.0);
    style.spacing.button_padding = Vec2::new(10.0, 6.0);
    style
        .text_styles
        .insert(egui::TextStyle::Body, FontId::proportional(13.0));
    style
        .text_styles
        .insert(egui::TextStyle::Button, FontId::proportional(12.0));
    style
        .text_styles
        .insert(egui::TextStyle::Small, FontId::proportional(11.0));
    ctx.set_style(style);
}

fn symbol_labels(model: &MemoryModel) -> Vec<Span> {
    // Build once when a model loads. Repeating source interval lookup on every
    // frame makes real firmware with thousands of symbols unnecessarily slow.
    let mut regions: BTreeMap<&str, Vec<&Span>> = BTreeMap::new();
    for span in &model.static_spans {
        regions.entry(&span.region).or_default().push(span);
    }
    for spans in regions.values_mut() {
        spans.sort_by_key(|span| (span.start, std::cmp::Reverse(span.size)));
    }
    model.symbols.iter().enumerate().map(|(index, symbol)| {
        let owner = regions.get(symbol.region.as_str()).and_then(|spans| {
            let end = symbol.start.checked_add(symbol.size)?;
            let limit = spans.partition_point(|span| span.start <= symbol.start);
            spans[..limit].iter().rev().find(|span| symbol.start < span.end() && end <= span.end()).copied()
        });
        Span {
            id: format!("elf-symbol:{index}"),
            name: symbol.name.clone(), region: symbol.region.clone(), start: symbol.start, size: symbol.size,
            kind: owner.map(|span| span.kind).unwrap_or(SpanKind::Unknown),
            source: owner.and_then(|span| span.source.clone()), parent: owner.map(|span| span.id.clone()),
            detail: Some("ELF symbol label. Symbols can overlap or alias; they do not add occupied bytes. A zero-size symbol is an address label with no recorded extent.".into()),
        }
    }).collect()
}

fn source_color(source: &str) -> Color32 {
    let hash = source.bytes().fold(0xcbf29ce484222325u64, |hash, byte| {
        (hash ^ byte as u64).wrapping_mul(0x100000001b3)
    });
    PALETTE[hash as usize % PALETTE.len()]
}

fn region_kind(kind: RegionKind) -> &'static str {
    match kind {
        RegionKind::Flash => "FLASH",
        RegionKind::Ram => "RAM",
        RegionKind::Unknown => "REGION",
    }
}

fn kind_name(kind: SpanKind) -> &'static str {
    match kind {
        SpanKind::Code => "CODE",
        SpanKind::ReadOnly => "READ ONLY",
        SpanKind::Data => "INITIALIZED DATA",
        SpanKind::Bss => "ZERO-INITIALIZED RAM",
        SpanKind::HeapArena => "HEAP ARENA",
        SpanKind::Allocation => "ALLOCATION",
        SpanKind::Free => "RECORDED FREE",
        SpanKind::Stack => "STACK RESERVATION",
        SpanKind::StackUsed => "STACK USAGE EVIDENCE",
        SpanKind::Pool => "POOL",
        SpanKind::Reserved => "RESERVED",
        SpanKind::Unknown => "UNKNOWN",
    }
}

fn parse_address(value: &str) -> Result<u64, String> {
    let text = value.trim().replace('_', "");
    let digits = text
        .strip_prefix("0x")
        .or_else(|| text.strip_prefix("0X"))
        .unwrap_or(&text);
    if digits.is_empty() {
        return Err("Enter a hexadecimal address.".into());
    }
    u64::from_str_radix(digits, 16)
        .map_err(|_| "Enter a hexadecimal address within the 64-bit address range.".into())
}

fn format_bytes(bytes: u64) -> String {
    if bytes < 1024 {
        format!("{bytes} B")
    } else if bytes < 1024 * 1024 {
        format!("{:.1} KiB", bytes as f64 / 1024.0)
    } else if bytes < 1024 * 1024 * 1024 {
        format!("{:.2} MiB", bytes as f64 / (1024.0 * 1024.0))
    } else {
        format!("{:.2} GiB", bytes as f64 / (1024.0 * 1024.0 * 1024.0))
    }
}

fn format_time(ns: u64) -> String {
    if ns < 1_000_000 {
        format!("{:.1} µs", ns as f64 / 1000.0)
    } else if ns < 1_000_000_000 {
        format!("{:.1} ms", ns as f64 / 1_000_000.0)
    } else {
        format!("{:.2} s", ns as f64 / 1_000_000_000.0)
    }
}

fn abbreviated(value: &str, max: usize) -> String {
    if value.chars().count() > max {
        format!("{}…", value.chars().take(max).collect::<String>())
    } else {
        value.into()
    }
}

fn short_source(source: &str) -> String {
    let pieces: Vec<_> = source.rsplit('/').take(2).collect();
    pieces.into_iter().rev().collect::<Vec<_>>().join("/")
}

fn eyebrow(ui: &mut egui::Ui, text: &str) {
    ui.label(RichText::new(text).size(10.0).strong().color(MUTED));
}

fn badge(ui: &mut egui::Ui, text: &str, color: Color32) {
    egui::Frame::new()
        .fill(color.linear_multiply(0.13))
        .stroke(Stroke::new(0.7, color.linear_multiply(0.45)))
        .corner_radius(4)
        .inner_margin(egui::Margin::symmetric(7, 4))
        .show(ui, |ui| {
            ui.label(RichText::new(text).size(9.0).strong().color(color));
        });
}

fn field(ui: &mut egui::Ui, label: &str, value: &str) {
    eyebrow(ui, label);
    ui.label(RichText::new(value).size(12.0));
    ui.add_space(10.0);
}

fn path_field(ui: &mut egui::Ui, label: &str, value: &mut String, hint: &str) {
    ui.label(RichText::new(label).small().color(MUTED));
    ui.add(
        egui::TextEdit::singleline(value)
            .hint_text(hint)
            .font(egui::TextStyle::Monospace)
            .desired_width(f32::INFINITY),
    );
}

fn paint_priority(kind: SpanKind) -> u8 {
    match kind {
        SpanKind::HeapArena | SpanKind::Pool | SpanKind::Stack => 0,
        SpanKind::StackUsed => 2,
        _ => 1,
    }
}

fn hatch(painter: &egui::Painter, rect: Rect, color: Color32) {
    let painter = painter.with_clip_rect(rect);
    let mut x = rect.left() - rect.height();
    while x < rect.right() {
        painter.line_segment(
            [
                Pos2::new(x, rect.bottom()),
                Pos2::new(x + rect.height(), rect.top()),
            ],
            Stroke::new(1.0, color),
        );
        x += 14.0;
    }
}

fn save_ppm(path: &Path, image: &egui::ColorImage) -> Result<(), String> {
    let file = std::fs::File::create(path)
        .map_err(|error| format!("Cannot save screenshot {}: {error}", path.display()))?;
    let mut writer = std::io::BufWriter::new(file);
    write!(writer, "P6\n{} {}\n255\n", image.width(), image.height())
        .map_err(|error| error.to_string())?;
    for pixel in &image.pixels {
        writer
            .write_all(&pixel.to_array()[..3])
            .map_err(|error| error.to_string())?;
    }
    writer.flush().map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn magnified_addresses_above_double_precision_are_distinct() {
        let start = 0xffff_0000_0000_0000;
        let view = AddressWindow {
            start,
            end: start + 1024,
        };
        assert_eq!(view.fraction(start + 1), 1.0 / 1024.0);
        assert_eq!(view.at_fraction(1.0 / 1024.0), start + 1);
    }

    #[test]
    fn zoom_anchors_and_pan_obey_declared_region() {
        let bounds = AddressWindow {
            start: 0x2000_0000,
            end: 0x2004_0000,
        };
        let anchor = bounds.start + 0x10000;
        let zoom = bounds.zoom(0.5, anchor, bounds);
        assert_eq!(zoom.span(), 0x20000);
        assert_eq!(zoom.fraction(anchor), bounds.fraction(anchor));
        assert_eq!(zoom.pan(-1000.0, bounds).start, bounds.start);
        assert_eq!(zoom.pan(1000.0, bounds).end, bounds.end);
    }

    #[test]
    fn address_search_never_rounds_or_truncates() {
        assert_eq!(
            parse_address("0xffff_ffff_ffff_fffe").unwrap(),
            u64::MAX - 1
        );
        assert!(parse_address("0x10000000000000000").is_err());
        assert!(parse_address("invalid").is_err());
    }

    #[test]
    fn high_address_pan_clamps_without_shrinking_window() {
        let bounds = AddressWindow {
            start: u64::MAX - 65536,
            end: u64::MAX,
        };
        let view = bounds.zoom(0.25, bounds.at_fraction(0.5), bounds);
        let panned = view.pan(1000000.0, bounds);
        assert_eq!(panned.end, bounds.end);
        assert_eq!(panned.span(), view.span());
    }

    #[test]
    fn checkpoint_gap_hides_runtime_but_preserves_linked_placement() {
        let mut app = MemoryExplorer::new(InputPaths::default(), true, None).unwrap();
        let captured = app.spans();
        assert!(captured
            .iter()
            .any(|span| span.span.kind == SpanKind::Allocation));
        app.time_ns = Some(500_000_000);
        assert!(!app.exact_snapshot());
        let unknown = app.spans();
        assert!(!unknown
            .iter()
            .any(|span| span.span.kind == SpanKind::Allocation));
        assert!(unknown.iter().any(|span| span.span.kind == SpanKind::Bss));
    }

    #[test]
    fn physical_aliases_use_one_full_declared_resource_axis() {
        let mut app = MemoryExplorer::new(InputPaths::default(), true, None).unwrap();
        app.switch_address_mode(true);
        let sram = app
            .regions()
            .into_iter()
            .find(|region| region.key == "sram")
            .unwrap();
        assert_eq!(sram.members.len(), 2);
        assert_eq!(
            sram.bounds,
            AddressWindow {
                start: 0,
                end: 8192
            }
        );
        app.select_region(&sram);
        let allocation = app
            .spans()
            .into_iter()
            .find(|span| span.span.kind == SpanKind::Allocation)
            .unwrap();
        assert_eq!(allocation.bounds.start, allocation.span.start - 0x2000_0000);
    }

    #[test]
    fn static_reserved_sections_do_not_disappear_without_runtime_capture() {
        let mut app = MemoryExplorer::new(InputPaths::default(), false, None).unwrap();
        let mut model = nxrs_memory_explorer::demo();
        let reserved = model
            .static_spans
            .iter_mut()
            .find(|span| span.id == "heap-storage")
            .unwrap();
        reserved.kind = SpanKind::Reserved;
        app.install_model(model, true);
        app.time_ns = None;
        let reserved = app
            .spans()
            .into_iter()
            .find(|span| span.span.id == "heap-storage")
            .unwrap();
        assert_eq!(reserved.lane(), 0);
        assert!(!reserved.runtime);
    }
}
