use super::*;
use nxrs_memory_explorer::Placement;

enum Destination {
    Backing {
        resource: String,
        span: Option<String>,
    },
    Placement(Placement),
    Region(String),
}

fn role_color(kind: SpanKind) -> Color32 {
    match kind {
        SpanKind::Code => Color32::from_rgb(100, 161, 235),
        SpanKind::ReadOnly => Color32::from_rgb(180, 137, 232),
        SpanKind::Data => Color32::from_rgb(84, 194, 177),
        SpanKind::Bss => Color32::from_rgb(136, 189, 116),
        SpanKind::Allocation => Color32::from_rgb(215, 127, 156),
        SpanKind::Stack | SpanKind::StackUsed => Color32::from_rgb(235, 172, 100),
        SpanKind::Free => Color32::from_rgb(48, 64, 79),
        SpanKind::HeapArena | SpanKind::Pool => Color32::from_rgb(33, 43, 58),
        SpanKind::Reserved => Color32::from_rgb(102, 118, 139),
        SpanKind::Unknown => Color32::from_rgb(91, 79, 56),
    }
}

fn total_bytes(bytes: u128) -> String {
    u64::try_from(bytes)
        .map(format_bytes)
        .unwrap_or_else(|_| format!("{bytes} B"))
}

fn delta_bytes(now: u64, before: u64) -> String {
    let delta = now as i128 - before as i128;
    format!("{delta:+} B")
}

impl MemoryExplorer {
    pub(super) fn overview(&mut self, ctx: &egui::Context) {
        let mut destination = None;
        egui::CentralPanel::default().frame(egui::Frame::new().fill(BACKGROUND).inner_margin(24)).show(ctx, |ui| {
            let Some(model) = &self.model else { return; };
            ui.heading("Board memory and linked placement");
            ui.label(RichText::new("Backing resources preserve physical capacity. Address windows and load copies describe how those bytes are used.").color(MUTED));
            ui.add_space(16.0);
            let runtime = self.runtime_spans();
            let occupied = model.physical_occupied_bytes_with_runtime(&runtime);
            let capacity: u128 = model.physical_resources.iter().map(|resource| resource.size as u128).sum();
            let occupied_total: u128 = occupied.values().map(|bytes| *bytes as u128).sum();
            ui.columns(3, |columns| {
                for (column, (label, value, detail)) in columns.iter_mut().zip([
                    ("DECLARED BACKING CAPACITY", if model.physical_resources.is_empty() { "Unknown".into() } else { total_bytes(capacity) }, format!("{} distinct resources; aliases counted once", model.physical_resources.len())),
                    ("MAPPED OCCUPIED STORAGE", if model.physical_resources.is_empty() { "Unknown".into() } else { total_bytes(occupied_total) }, "Union of linked placement and selected memory evidence".into()),
                    ("LOAD / RUNTIME RELATIONSHIPS", model.load_mappings.len().to_string(), "ELF file-backed bytes mapped to linked addresses".into()),
                ]) {
                    egui::Frame::new().fill(SURFACE).corner_radius(7).inner_margin(12).show(column, |ui| {
                        ui.set_min_width(ui.available_width());
                        ui.label(RichText::new(label).size(9.0).color(MUTED));
                        ui.label(RichText::new(value).size(24.0).strong());
                        ui.label(RichText::new(detail).small().color(MUTED));
                    });
                }
            });
            ui.add_space(14.0);
            ui.horizontal_wrapped(|ui| {
                for (kind, label) in [
                    (SpanKind::Code, "Code"), (SpanKind::ReadOnly, "Read-only"), (SpanKind::Data, "Data / initializer"),
                    (SpanKind::Bss, "BSS"), (SpanKind::Allocation, "Allocated"), (SpanKind::Stack, "Stack overlay"),
                    (SpanKind::Free, "Known free"), (SpanKind::Reserved, "Reserved"), (SpanKind::Unknown, "Unobserved"),
                ] {
                    let (rect, _) = ui.allocate_exact_size(Vec2::splat(9.0), Sense::hover());
                    ui.painter().rect_filled(rect, 2, role_color(kind));
                    ui.label(RichText::new(label).size(10.0).color(MUTED));
                }
            });
            ui.add_space(10.0);
            egui::ScrollArea::vertical().id_salt("board-overview-scroll").show(ui, |ui| {
                let mut physical = model.physical_occupied_intervals_with_runtime(&runtime);
                physical.extend(model.physical_intervals_with_runtime(&runtime).into_iter().filter(|entry| entry.span.kind == SpanKind::Free));
                physical.sort_by_key(|entry| (paint_priority(entry.span.kind), std::cmp::Reverse(entry.span.size)));
                if model.physical_resources.is_empty() {
                    egui::Frame::new().fill(PANEL).stroke(Stroke::new(1.0, LINE)).corner_radius(8).inner_margin(18).show(ui, |ui| {
                        ui.heading("Physical backing is not declared");
                        ui.label("The ELF establishes linked CPU and load addresses. It cannot establish the board's complete capacity, bank topology or alias relationships on its own.");
                        ui.label(RichText::new("Supply a verified layout to enable the bank overview. CPU-address exploration remains available below.").color(WARNING));
                    });
                }
                for resource in &model.physical_resources {
                    egui::Frame::new().fill(PANEL).stroke(Stroke::new(1.0, LINE)).corner_radius(8).inner_margin(14).show(ui, |ui| {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(&resource.name).size(16.0).strong());
                            ui.label(RichText::new(format_bytes(resource.size)).color(ACCENT));
                            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                if ui.button("Inspect bytes").clicked() { destination = Some(Destination::Backing { resource: resource.id.clone(), span: None }); }
                            });
                        });
                        let aliases: Vec<_> = model.regions.iter().filter(|region| region.physical.as_ref().is_some_and(|backing| backing.id == resource.id)).collect();
                        ui.horizontal_wrapped(|ui| {
                            ui.label(RichText::new("Mappings").small().color(MUTED));
                            for region in &aliases {
                                let domain = match region.domain { AddressDomain::CpuVirtual => "CPU", AddressDomain::LinkedLoad => "Load", AddressDomain::Image => "File", AddressDomain::Physical => "Backing" };
                                if ui.small_button(format!("{domain} {:#x}..{:#x}", region.start, region.end())).on_hover_text(&region.name).clicked() {
                                    destination = Some(Destination::Region(region.id.clone()));
                                }
                            }
                        });
                        let (rect, response) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 86.0), Sense::click());
                        let band = Rect::from_min_max(rect.min + Vec2::new(0.0, 23.0), rect.max - Vec2::new(0.0, 17.0));
                        let axis = AddressWindow { start: 0, end: resource.size };
                        let at = |offset| band.left() + axis.fraction(offset) as f32 * band.width();
                        let painter = ui.painter().with_clip_rect(rect);
                        painter.rect_filled(band, 3, role_color(SpanKind::Unknown).linear_multiply(0.25));
                        hatch(&painter.with_clip_rect(band), band, LINE);
                        let entries: Vec<_> = physical.iter().filter(|entry| entry.resource == resource.id).collect();
                        let mut hit = None;
                        for entry in &entries {
                            let lower = matches!(entry.span.kind, SpanKind::Stack | SpanKind::StackUsed);
                            let block = Rect::from_min_max(
                                Pos2::new(at(entry.range.start), if lower { band.center().y + 4.0 } else { band.top() }),
                                Pos2::new(at(entry.range.end).max(at(entry.range.start) + 0.8).min(band.right()), if lower { band.bottom() } else { band.center().y + 1.0 }),
                            );
                            painter.rect_filled(block, 1, role_color(entry.span.kind));
                            if response.hover_pos().is_some_and(|pointer| block.expand2(Vec2::new(2.0, 0.0)).contains(pointer)) { hit = Some(*entry); }
                        }
                        for bank in &resource.banks {
                            let a = at(bank.offset);
                            let b = at(bank.offset + bank.size);
                            painter.line_segment([Pos2::new(a, rect.top() + 19.0), Pos2::new(a, band.bottom())], Stroke::new(1.0, MUTED));
                            painter.with_clip_rect(Rect::from_min_max(Pos2::new(a + 4.0, rect.top()), Pos2::new(b - 4.0, band.top()))).text(Pos2::new((a + b) / 2.0, rect.top() + 4.0), Align2::CENTER_TOP, format!("{} · {}", bank.name, format_bytes(bank.size)), FontId::proportional(10.0), MUTED);
                        }
                        painter.text(Pos2::new(band.left(), band.bottom() + 4.0), Align2::LEFT_TOP, "backing offset 0x0", FontId::monospace(9.0), MUTED);
                        painter.text(Pos2::new(band.right(), band.bottom() + 4.0), Align2::RIGHT_TOP, format!("{:#x}", resource.size), FontId::monospace(9.0), MUTED);
                        if let Some(entry) = hit {
                            response.clone().on_hover_ui(|ui| {
                                ui.strong(&entry.span.name);
                                ui.label(format!("Backing {:#x} .. {:#x}", entry.range.start, entry.range.end));
                                ui.label(format!("{} · {}", kind_name(entry.span.kind), format_bytes(entry.range.size())));
                                ui.label("Click to inspect this range.");
                            });
                            if response.clicked() { destination = Some(Destination::Backing { resource: resource.id.clone(), span: Some(entry.span.id.clone()) }); }
                        }
                        ui.label(RichText::new(format!("{} occupied; remaining space includes observed free and unobserved bytes. Each resource has its own linear scale.", format_bytes(*occupied.get(&resource.id).unwrap_or(&0)))).small().color(MUTED));
                    });
                    ui.add_space(10.0);
                }
                ui.add_space(10.0);
                ui.heading("Linked load and runtime locations");
                ui.label(RichText::new("These are link-time relationships. Explicit backing mappings establish whether load and runtime addresses use distinct storage or alias the same bytes; no startup copy execution is inferred.").small().color(MUTED));
                ui.add_space(8.0);
                egui::Grid::new("load-runtime-overview").num_columns(4).striped(true).spacing([20.0, 10.0]).show(ui, |ui| {
                    for title in ["Section", "Runtime CPU", "Linked load", "ELF container"] { ui.strong(title); }
                    ui.end_row();
                    for mapping in &model.load_mappings {
                        ui.label(format!("{} · {}", mapping.section, format_bytes(mapping.runtime.size)));
                        for placement in [&mapping.runtime, &mapping.load, &mapping.image] {
                            if ui.link(format!("{:#x} .. {:#x}", placement.start, placement.start + placement.size)).clicked() {
                                destination = Some(Destination::Placement(placement.clone()));
                            }
                        }
                        ui.end_row();
                    }
                });
                ui.add_space(7.0);
                ui.label(RichText::new("BSS consumes runtime RAM without file-backed initializer bytes. ELF-container offsets are not programmed-image offsets or vendor headers.").small().color(WARNING));
                if model.physical_resources.is_empty() {
                    ui.add_space(18.0);
                    ui.heading("Linked address windows");
                    for region in &model.regions {
                        if ui.button(format!("{} · {:#x} .. {:#x}", region.name, region.start, region.end())).clicked() { destination = Some(Destination::Region(region.id.clone())); }
                    }
                }
                ui.add_space(18.0);
                egui::CollapsingHeader::new("Mapping provenance and capture limits").show(ui, |ui| {
                    for warning in &model.warnings { ui.label(RichText::new(warning).small().color(WARNING)); }
                });
            });
        });
        if let Some(destination) = destination {
            self.navigate_destination(destination);
        }
    }

    fn navigate_destination(&mut self, destination: Destination) {
        self.view = ViewMode::Memory;
        match destination {
            Destination::Backing { resource, span } => {
                self.physical = true;
                if let Some(region) = self
                    .regions()
                    .into_iter()
                    .find(|region| region.key == resource)
                {
                    self.select_region(&region);
                    if let Some(block) = span
                        .and_then(|id| self.spans().into_iter().find(|block| block.span.id == id))
                    {
                        self.focus(&block);
                    }
                }
            }
            Destination::Region(id) => {
                self.physical = false;
                if let Some(region) = self.regions().into_iter().find(|region| region.key == id) {
                    self.select_region(&region);
                }
            }
            Destination::Placement(placement) => {
                self.physical = false;
                if let Some(region) = self
                    .regions()
                    .into_iter()
                    .find(|region| region.key == placement.region)
                {
                    self.select_region(&region);
                    let margin = placement.size / 4;
                    self.viewport = AddressWindow {
                        start: placement.start.saturating_sub(margin),
                        end: placement
                            .start
                            .saturating_add(placement.size)
                            .saturating_add(margin),
                    }
                    .fit_inside(region.bounds);
                    self.selected = self
                        .spans()
                        .into_iter()
                        .find(|block| {
                            block.span.start == placement.start
                                && block.span.size == placement.size
                                && !block.symbol
                        })
                        .map(|block| block.span.id);
                }
            }
        }
    }

    pub(super) fn load_mapping_inspector(&mut self, ui: &mut egui::Ui, span: &Span) {
        let mut destination = None;
        if let Some(model) = &self.model {
            let matching: Vec<_> = model
                .load_mappings
                .iter()
                .filter(|mapping| {
                    [&mapping.runtime, &mapping.load, &mapping.image]
                        .iter()
                        .any(|placement| {
                            placement.region == span.region
                                && span.start >= placement.start
                                && span.end() <= placement.start + placement.size
                        })
                })
                .collect();
            if !matching.is_empty() {
                egui::CollapsingHeader::new("Load / runtime relationship")
                    .default_open(true)
                    .show(ui, |ui| {
                        for mapping in matching {
                            ui.strong(&mapping.section);
                            for (name, placement) in [
                                ("Runtime CPU", &mapping.runtime),
                                ("Linked load", &mapping.load),
                                ("ELF file bytes", &mapping.image),
                            ] {
                                if ui.link(format!("{name}: {:#x}", placement.start)).clicked() {
                                    destination = Some(Destination::Placement(placement.clone()));
                                }
                            }
                        }
                        ui.label(
                            RichText::new(
                                "Link-time placement; this does not observe a startup copy.",
                            )
                            .small()
                            .color(MUTED),
                        );
                    });
            } else if span.kind == SpanKind::Bss {
                ui.label(RichText::new("Zero-initialized runtime storage. This section has no file-backed initializer copy.").small().color(MUTED));
            }
        }
        if let Some(destination) = destination {
            self.navigate_destination(destination);
        }
    }

    pub(super) fn comparison_controls(&mut self, ui: &mut egui::Ui) {
        if self.replay_active() {
            return;
        }
        let Some(model) = &self.model else {
            return;
        };
        if model.snapshots.len() < 2 {
            return;
        }
        ui.add_space(8.0);
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new("Compare with").small().color(MUTED));
            let current = self
                .comparison
                .and_then(|time| model.snapshot_at(time))
                .map(|snapshot| snapshot.label.as_str())
                .unwrap_or("No comparison");
            egui::ComboBox::from_id_salt("memory-baseline")
                .selected_text(current)
                .show_ui(ui, |ui| {
                    ui.selectable_value(&mut self.comparison, None, "No comparison");
                    for snapshot in &model.snapshots {
                        ui.selectable_value(
                            &mut self.comparison,
                            Some(snapshot.time_ns),
                            &snapshot.label,
                        );
                    }
                });
        });
        let (Some(before), Some(now), Some(region)) =
            (self.comparison, self.time_ns, self.current_region())
        else {
            return;
        };
        if model.snapshot_at(now).is_none() {
            ui.label(
                RichText::new("Comparison unavailable between observed checkpoints.")
                    .small()
                    .color(WARNING),
            );
            return;
        }
        let mut previous = BTreeMap::new();
        let mut current = BTreeMap::new();
        for member in &region.members {
            for stats in model.heap_statistics(member, before) {
                previous.insert(stats.arena.clone(), stats);
            }
            for stats in model.heap_statistics(member, now) {
                current.insert(stats.arena.clone(), stats);
            }
        }
        for (id, stats) in &current {
            if let Some(old) = previous.get(id) {
                ui.label(
                    RichText::new(format!(
                        "Allocated {}   Free {}   Largest free {}",
                        delta_bytes(stats.allocated_chunk_bytes, old.allocated_chunk_bytes),
                        delta_bytes(stats.free_chunk_bytes, old.free_chunk_bytes),
                        delta_bytes(stats.largest_free_chunk, old.largest_free_chunk)
                    ))
                    .small()
                    .color(ACCENT),
                );
                if current.len() > 1 {
                    ui.label(RichText::new(id).small().color(MUTED));
                }
            }
        }
        ui.label(RichText::new("Endpoint differences only. Address reuse does not establish allocation identity; intervening events remain unknown.").small().color(MUTED));
    }
}
