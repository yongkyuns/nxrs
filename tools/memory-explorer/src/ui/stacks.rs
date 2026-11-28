//! Stack measurements belong to one exact checkpoint. A repeated task ID or
//! address does not establish a task lifetime or fill a gap between captures.

use super::*;
use nxrs_memory_explorer::{
    AddressRange, Snapshot, StackCurrent, StackGrowth, StackHighWater, StackObservation,
    StackPointerSource,
};

const CURRENT: Color32 = Color32::from_rgb(100, 161, 235);
const TOUCHED: Color32 = Color32::from_rgb(235, 172, 100);

#[derive(Clone, Copy)]
struct StackRow {
    span: usize,
    observation: Option<usize>,
}

impl StackRow {
    fn evidence(self, snapshot: &Snapshot) -> Option<&StackObservation> {
        self.observation
            .map(|index| &snapshot.stack_observations[index])
    }
}

#[derive(Default)]
pub(super) struct StackView {
    // Cache indices, not a second copy of stack payloads or rendered rows.
    rows: BTreeMap<u64, Vec<StackRow>>,
    selected: Option<(u64, String)>,
}

impl StackView {
    pub(super) fn from_model(model: &MemoryModel) -> Self {
        let rows = model
            .snapshots
            .iter()
            .map(|snapshot| {
                let observations: BTreeMap<_, _> = snapshot
                    .stack_observations
                    .iter()
                    .enumerate()
                    .map(|(index, observation)| (observation.stack.as_str(), index))
                    .collect();
                let rows = snapshot
                    .spans
                    .iter()
                    .enumerate()
                    .filter(|(_, span)| span.kind == SpanKind::Stack)
                    .map(|(index, span)| StackRow {
                        span: index,
                        observation: observations.get(span.id.as_str()).copied(),
                    })
                    .collect();
                (snapshot.time_ns, rows)
            })
            .collect();
        Self {
            rows,
            selected: None,
        }
    }
}

impl MemoryExplorer {
    pub(super) fn open_stack_usage(&mut self, span: &Span) {
        let id = if span.kind == SpanKind::Stack {
            Some(span.id.as_str())
        } else {
            span.parent.as_deref()
        };
        if let Some((snapshot, rows)) = self.stack_rows() {
            if let Some(row) = rows
                .iter()
                .find(|row| Some(snapshot.spans[row.span].id.as_str()) == id)
            {
                self.stacks.selected =
                    Some((snapshot.time_ns, snapshot.spans[row.span].id.clone()));
                self.view = ViewMode::Stacks;
            }
        }
    }

    fn stack_rows(&self) -> Option<(&Snapshot, &[StackRow])> {
        if self.replay_active() {
            return None;
        }
        let time = self.time_ns?;
        let snapshot = self.model.as_ref()?.snapshot_at(time)?;
        Some((snapshot, self.stacks.rows.get(&time)?.as_slice()))
    }

    fn stack_checkpoint_controls(&mut self, ui: &mut egui::Ui) {
        let snapshots: Vec<_> = self
            .model
            .iter()
            .flat_map(|model| &model.snapshots)
            .map(|snapshot| (snapshot.time_ns, snapshot.label.clone()))
            .collect();
        let replay = self.replay_active();
        let label = if replay {
            "Heap replay selected"
        } else {
            snapshots
                .iter()
                .find(|(time, _)| Some(*time) == self.time_ns)
                .map(|(_, label)| label.as_str())
                .unwrap_or("No checkpoint selected")
        };
        ui.horizontal_wrapped(|ui| {
            eyebrow(ui, "CAPTURED CHECKPOINT");
            ui.add_enabled_ui(!replay, |ui| {
                egui::ComboBox::from_id_salt("stack-checkpoint")
                    .selected_text(label)
                    .width(310.0)
                    .show_ui(ui, |ui| {
                        for (index, (time, label)) in snapshots.iter().enumerate() {
                            if ui
                                .selectable_label(
                                    self.time_ns == Some(*time),
                                    format!("{} · {label}", index + 1),
                                )
                                .clicked()
                            {
                                self.time_ns = Some(*time);
                            }
                        }
                    });
                let previous = snapshots
                    .iter()
                    .any(|(time, _)| self.time_ns.is_some_and(|now| *time < now));
                let next = snapshots
                    .iter()
                    .any(|(time, _)| self.time_ns.is_some_and(|now| *time > now));
                if ui
                    .add_enabled(previous, egui::Button::new("‹ Previous"))
                    .clicked()
                {
                    self.adjacent_snapshot(false);
                }
                if ui.add_enabled(next, egui::Button::new("Next ›")).clicked() {
                    self.adjacent_snapshot(true);
                }
            });
            if let Some(link) = self
                .correlation
                .as_ref()
                .and_then(|correlation| {
                    self.time_ns
                        .and_then(|time| correlation.link_for_snapshot(time))
                })
                .filter(|_| !replay)
            {
                let time = link.trace_time_ns;
                if ui.small_button("Show in timing").clicked() {
                    self.open_timing_at(time);
                }
            }
        });
    }

    pub(super) fn stack_usage(&mut self, ctx: &egui::Context) {
        let mut select = None;
        let mut open = None;
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(BACKGROUND).inner_margin(20))
            .show(ctx, |ui| {
                ui.push_id(self.widget_id("stack-usage"), |ui| {
                    self.stack_checkpoint_controls(ui);
                    ui.add_space(12.0);
                    ui.heading("Task stacks");
                    ui.label(RichText::new("Compare reserved space, stack-pointer depth, and the touched coloration watermark at this checkpoint.").color(MUTED));
                    ui.label(RichText::new("Point observations only · intervening use and task lifetimes are unknown").small().color(MUTED));
                    ui.add_space(14.0);
                    let Some((snapshot, rows)) = self.stack_rows() else {
                        let message = if self.replay_active() {
                            "Heap replay does not establish stack state. Select Point snapshots above to inspect a captured checkpoint."
                        } else if self.model.as_ref().is_some_and(|model| model.snapshots.is_empty()) {
                            "This recording has no stack checkpoints. Load a stopped-target snapshot to inspect its observed task reservations and measurements."
                        } else {
                            "Stack state is unknown at this time. Choose a captured checkpoint; stack measurements are not carried forward between observations."
                        };
                        ui.label(RichText::new(message).color(WARNING));
                        return;
                    };
                    if rows.is_empty() {
                        ui.label("No task stack reservations were captured at this checkpoint.");
                        return;
                    }
                    let current_count = rows.iter().filter(|row| row.evidence(snapshot).and_then(|value| value.current_used(&snapshot.spans[row.span])).is_some()).count();
                    let watermark_count = rows.iter().filter(|row| row.evidence(snapshot).and_then(StackObservation::watermark_used).is_some()).count();
                    ui.horizontal_wrapped(|ui| {
                        badge(ui, "OBSERVED POINT", ACCENT);
                        ui.label(format!("Stacks: {} · Pointer measurements: {current_count} · Coloration scans: {watermark_count}", rows.len()));
                    });
                    ui.add_space(10.0);
                    let selected = self.stacks.selected.as_ref()
                        .filter(|(time, _)| *time == snapshot.time_ns)
                        .and_then(|(_, id)| rows.iter().find(|row| snapshot.spans[row.span].id == *id))
                        .or_else(|| rows.first());
                    let selected_id = selected.map(|row| snapshot.spans[row.span].id.as_str());
                    let table_height = (ui.available_height() - 330.0).clamp(110.0, 285.0)
                        .min(rows.len() as f32 * 66.0 + 5.0);
                    egui::Frame::new().fill(PANEL).corner_radius(7).inner_margin(10).show(ui, |ui| {
                        table_heading(ui);
                        ui.separator();
                        egui::ScrollArea::vertical().id_salt("stack-rows")
                            .max_height(table_height).auto_shrink([false, false])
                            .show_rows(ui, 59.0, rows.len(), |ui, range| {
                                for row in &rows[range] {
                                    let span = &snapshot.spans[row.span];
                                    let evidence = row.evidence(snapshot);
                                    if draw_row(ui, span, evidence, selected_id == Some(span.id.as_str())).clicked() {
                                        select = Some((snapshot.time_ns, span.id.clone()));
                                    }
                                }
                            });
                    });
                    ui.add_space(14.0);
                    if let Some(row) = selected {
                        let span = &snapshot.spans[row.span];
                        egui::ScrollArea::vertical().id_salt("stack-detail")
                            .auto_shrink([false, false]).show(ui, |ui| {
                                ui.horizontal_wrapped(|ui| {
                                    ui.heading(&span.name);
                                    if ui.button("Open in memory").clicked() {
                                        open = Some(span.id.clone());
                                    }
                                });
                                stack_detail(ui, span, row.evidence(snapshot));
                            });
                    }
                });
            });
        if let Some(selection) = select {
            self.stacks.selected = Some(selection);
        }
        if let Some(id) = open {
            self.open_stack_memory(&id);
        }
    }

    fn open_stack_memory(&mut self, id: &str) {
        let Some(span) = self.stack_rows().and_then(|(snapshot, _)| {
            snapshot
                .spans
                .iter()
                .find(|span| span.id == id && span.kind == SpanKind::Stack)
                .cloned()
        }) else {
            return;
        };
        self.physical = false;
        if let Some(region) = self
            .regions()
            .into_iter()
            .find(|region| region.key == span.region)
        {
            self.select_region(&region);
            if let Some(display) = self
                .spans()
                .into_iter()
                .find(|value| value.span.id == id && value.runtime)
            {
                self.focus(&display);
                self.view = ViewMode::Memory;
            }
        }
    }

    pub(super) fn smoke_stack_navigation(&mut self) -> Result<(), String> {
        if self.replay_active() {
            return if self.stack_rows().is_none() {
                Ok(())
            } else {
                Err("Replay exposed stack measurements".into())
            };
        }
        let saved_time = self.time_ns;
        let first = self
            .model
            .as_ref()
            .and_then(|model| model.snapshots.first())
            .map(|snapshot| snapshot.time_ns);
        let second = self
            .model
            .as_ref()
            .and_then(|model| model.snapshots.get(1))
            .map(|snapshot| snapshot.time_ns);
        if let (Some(first), Some(second)) = (first, second) {
            if second > first + 1 {
                self.time_ns = Some(first + (second - first) / 2);
                if self.stack_rows().is_some() {
                    return Err("Stack measurements leaked between checkpoints".into());
                }
            }
            self.time_ns = Some(first);
            self.adjacent_snapshot(true);
            if self.time_ns != Some(second) {
                return Err("Stack checkpoint navigation missed the next observation".into());
            }
        }
        self.time_ns = saved_time.or(first);
        let stack = self
            .stack_rows()
            .and_then(|(snapshot, rows)| rows.first().map(|row| snapshot.spans[row.span].clone()));
        if let Some(stack) = stack {
            self.open_stack_memory(&stack.id);
            if self.view != ViewMode::Memory
                || self.selected.as_deref() != Some(&stack.id)
                || self.viewport.start > stack.start
                || self.viewport.end < stack.end()
            {
                return Err("Stack address navigation changed the captured interval".into());
            }
            self.stacks.selected = self.time_ns.map(|time| (time, stack.id));
        }
        self.view = ViewMode::Stacks;
        Ok(())
    }
}

fn table_heading(ui: &mut egui::Ui) {
    let width = ui.available_width();
    let (rect, _) = ui.allocate_exact_size(Vec2::new(width, 21.0), Sense::hover());
    for (fraction, title) in [
        (0.0, "TASK / STACK"),
        (0.33, "RESERVED"),
        (0.48, "CURRENT DEPTH"),
        (0.72, "TOUCHED WATERMARK"),
    ] {
        ui.painter().text(
            rect.left_top() + Vec2::new(8.0 + width * fraction, 3.0),
            Align2::LEFT_TOP,
            title,
            FontId::proportional(10.0),
            MUTED,
        );
    }
}

fn draw_row(
    ui: &mut egui::Ui,
    span: &Span,
    evidence: Option<&StackObservation>,
    selected: bool,
) -> egui::Response {
    let width = ui.available_width();
    let (rect, response) = ui.allocate_exact_size(Vec2::new(width, 59.0), Sense::click());
    let painter = ui.painter().with_clip_rect(rect);
    painter.rect_filled(
        rect,
        4,
        if selected {
            Color32::from_rgb(29, 55, 59)
        } else if response.hovered() {
            SURFACE
        } else {
            PANEL
        },
    );
    let text = |fraction: f32, y, value: String, color, strong| {
        let clip = Rect::from_min_max(
            Pos2::new(rect.left() + width * fraction + 7.0, rect.top()),
            Pos2::new(
                rect.left()
                    + width
                        * if fraction == 0.0 {
                            0.33
                        } else if fraction == 0.33 {
                            0.48
                        } else if fraction == 0.48 {
                            0.72
                        } else {
                            1.0
                        }
                    - 7.0,
                rect.bottom(),
            ),
        );
        painter.with_clip_rect(clip).text(
            Pos2::new(clip.left(), rect.top() + y),
            Align2::LEFT_TOP,
            value,
            FontId::proportional(if strong { 13.0 } else { 10.5 }),
            color,
        );
    };
    text(
        0.0,
        9.0,
        span.name.clone(),
        if selected { ACCENT } else { TEXT },
        true,
    );
    text(
        0.0,
        33.0,
        evidence
            .map(|value| {
                format!(
                    "Task {} · {:#x} – {:#x}",
                    value.task_id,
                    span.start,
                    span.end()
                )
            })
            .unwrap_or_else(|| format!("{:#x} – {:#x}", span.start, span.end())),
        MUTED,
        false,
    );
    text(0.33, 9.0, format_bytes(span.size), TEXT, true);
    text(0.33, 33.0, "usable extent".into(), MUTED, false);
    text(
        0.48,
        9.0,
        evidence
            .and_then(|value| value.current_used(span))
            .map(format_bytes)
            .unwrap_or_else(|| "Unknown".into()),
        CURRENT,
        true,
    );
    text(
        0.48,
        33.0,
        evidence
            .and_then(|value| value.current_headroom(span))
            .map(|bytes| format!("{} headroom", format_bytes(bytes)))
            .unwrap_or_else(|| "No qualified stack pointer".into()),
        MUTED,
        false,
    );
    text(
        0.72,
        9.0,
        evidence
            .and_then(StackObservation::watermark_used)
            .map(format_bytes)
            .unwrap_or_else(|| "Unknown".into()),
        TOUCHED,
        true,
    );
    text(
        0.72,
        33.0,
        evidence
            .and_then(StackObservation::watermark_headroom)
            .map(|bytes| format!("{} untouched in scan", format_bytes(bytes)))
            .unwrap_or_else(|| "No qualified coloration scan".into()),
        MUTED,
        false,
    );
    response.on_hover_text(&span.name)
}

fn stack_detail(ui: &mut egui::Ui, span: &Span, evidence: Option<&StackObservation>) {
    ui.label(
        RichText::new(format!(
            "Usable reservation {} B · CPU range [{:#x}, {:#x}) · region {}",
            span.size,
            span.start,
            span.end(),
            span.region
        ))
        .small()
        .color(MUTED),
    );
    let Some(evidence) = evidence else {
        ui.add_space(10.0);
        ui.label(RichText::new("This checkpoint records the reservation only. Current depth and touched watermark are unknown.").color(WARNING));
        ui.label(RichText::new("Legacy stack_used spans do not identify which measurement they represent; they remain visible in Memory detail.").small().color(MUTED));
        return;
    };
    ui.label(
        RichText::new(format!(
            "Task {} · TCB {:#x} · stack grows {}",
            evidence.task_id,
            evidence.tcb,
            match evidence.growth {
                StackGrowth::Down => "toward lower addresses",
                StackGrowth::Up => "toward higher addresses",
            }
        ))
        .small()
        .color(MUTED),
    );
    ui.add_space(8.0);
    let scan = match evidence.high_water {
        StackHighWater::Coloration {
            scan_start,
            scan_size,
            ..
        } => AddressRange::new(scan_start, scan_size).ok(),
        _ => None,
    };
    draw_band(
        ui,
        "Current depth",
        span,
        Some(span.range().expect("validated stack")),
        evidence.current_range(span),
        evidence.current_used(span).is_some(),
        CURRENT,
    );
    draw_band(
        ui,
        "Touched watermark",
        span,
        scan,
        evidence.watermark_range(),
        evidence.watermark_used().is_some(),
        TOUCHED,
    );
    ui.add_space(8.0);
    ui.columns(2, |columns| {
        match &evidence.current {
            StackCurrent::Observed { sp, source } => {
                field(&mut columns[0], "STACK POINTER", &format!("{sp:#x} · {}", match source { StackPointerSource::LiveRegister => "stopped CPU register", StackPointerSource::SavedContext => "saved task context" }));
                field(&mut columns[0], "CURRENT DEPTH / HEADROOM", &format!("{} B / {} B", evidence.current_used(span).unwrap(), evidence.current_headroom(span).unwrap()));
            }
            StackCurrent::Unavailable { reason } => field(&mut columns[0], "CURRENT DEPTH UNKNOWN", reason),
        }
        match &evidence.high_water {
            StackHighWater::Coloration { scan_start, scan_size, used_bytes, pattern, word_bytes } => {
                field(&mut columns[1], "TOUCHED / UNTOUCHED IN SCAN", &format!("{} B / {} B", used_bytes, evidence.watermark_headroom().unwrap()));
                field(&mut columns[1], "COLORATION COVERAGE", &format!("[{:#x}, {:#x}) · {} of {} B scanned\n{}-byte words · pattern {pattern:#x} · {} B outside scan", scan_start, scan_start + scan_size, scan_size, span.size, word_bytes, span.size - scan_size));
            }
            StackHighWater::Unavailable { reason } => field(&mut columns[1], "TOUCHED WATERMARK UNKNOWN", reason),
        }
    });
    ui.label(RichText::new("Coloration records touched memory. A stack pointer can reserve bytes without touching them, and a write can match the color pattern. Untouched bytes are an observation, not guaranteed future headroom.").small().color(MUTED));
}

fn draw_band(
    ui: &mut egui::Ui,
    label: &str,
    span: &Span,
    scanned: Option<AddressRange>,
    used: Option<AddressRange>,
    known: bool,
    color: Color32,
) {
    let (rect, _) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 29.0), Sense::hover());
    let painter = ui.painter().with_clip_rect(rect);
    painter.text(
        rect.left_center(),
        Align2::LEFT_CENTER,
        label,
        FontId::proportional(11.0),
        color,
    );
    let bar = Rect::from_min_max(
        Pos2::new(rect.left() + 145.0, rect.top() + 4.0),
        Pos2::new(rect.right(), rect.bottom() - 4.0),
    );
    painter.rect_filled(bar, 3, SURFACE);
    let subrange = |range: AddressRange| {
        Rect::from_min_max(
            Pos2::new(
                bar.left()
                    + ((range.start - span.start) as f64 / span.size as f64) as f32 * bar.width(),
                bar.top(),
            ),
            Pos2::new(
                bar.left()
                    + ((range.end - span.start) as f64 / span.size as f64) as f32 * bar.width(),
                bar.bottom(),
            ),
        )
    };
    if known {
        hatch(&painter, bar, LINE);
        if let Some(scan) = scanned {
            painter.rect_filled(subrange(scan), 2, Color32::from_rgb(40, 52, 67));
        }
        if let Some(used) = used {
            painter.rect_filled(subrange(used), 2, color);
        }
    } else {
        hatch(&painter, bar, LINE);
        painter.text(
            bar.center(),
            Align2::CENTER_CENTER,
            "Unknown",
            FontId::proportional(10.0),
            MUTED,
        );
    }
    painter.rect_stroke(bar, 3, Stroke::new(1.0, LINE), StrokeKind::Inside);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn measured_model() -> MemoryModel {
        let mut model = nxrs_memory_explorer::demo();
        for snapshot in &mut model.snapshots {
            snapshot.stack_observations.clear();
        }
        let snapshot = &mut model.snapshots[0];
        let span = snapshot
            .spans
            .iter()
            .find(|span| span.kind == SpanKind::Stack)
            .unwrap();
        snapshot.stack_observations.push(StackObservation {
            stack: span.id.clone(),
            task_id: 42,
            tcb: 0x2000_0100,
            growth: StackGrowth::Down,
            current: StackCurrent::Observed {
                sp: span.end() - 256,
                source: StackPointerSource::SavedContext,
            },
            high_water: StackHighWater::Coloration {
                scan_start: span.start + 8,
                scan_size: span.size - 16,
                used_bytes: 64,
                pattern: 0xdead_beef,
                word_bytes: 4,
            },
        });
        model.validate().unwrap();
        model
    }

    fn app(model: MemoryModel) -> MemoryExplorer {
        let mut app = MemoryExplorer::new(InputPaths::default(), false, None).unwrap();
        app.install_model(model, true);
        app.time_ns = Some(0);
        app.view = ViewMode::Stacks;
        app
    }

    #[test]
    fn legacy_reservations_do_not_invent_measurements_from_usage_spans() {
        let mut model = measured_model();
        for snapshot in &mut model.snapshots {
            snapshot.stack_observations.clear();
        }
        let app = app(model);
        let (snapshot, rows) = app.stack_rows().unwrap();
        assert!(!rows.is_empty());
        assert!(rows.iter().all(|row| row.evidence(snapshot).is_none()));
        assert!(snapshot
            .spans
            .iter()
            .any(|span| span.kind == SpanKind::StackUsed));
    }

    #[test]
    fn stack_metrics_preserve_current_depth_above_touched_watermark_and_scan_edges() {
        let app = app(measured_model());
        let (snapshot, rows) = app.stack_rows().unwrap();
        let row = rows[0];
        let span = &snapshot.spans[row.span];
        let observation = row.evidence(snapshot).unwrap();
        assert_eq!(observation.current_used(span), Some(256));
        assert_eq!(observation.current_headroom(span), Some(span.size - 256));
        assert_eq!(observation.watermark_used(), Some(64));
        assert_eq!(observation.watermark_headroom(), Some(span.size - 80));
        assert_eq!(observation.watermark_range().unwrap().end, span.end() - 8);
    }

    #[test]
    fn current_unknown_and_zero_coloration_remain_different_states() {
        let mut model = measured_model();
        let observation = &mut model.snapshots[0].stack_observations[0];
        observation.current = StackCurrent::Unavailable {
            reason: "Stopped in IRQ 16".into(),
        };
        if let StackHighWater::Coloration { used_bytes, .. } = &mut observation.high_water {
            *used_bytes = 0;
        }
        let app = app(model);
        let (snapshot, rows) = app.stack_rows().unwrap();
        let row = rows[0];
        let observation = row.evidence(snapshot).unwrap();
        assert_eq!(observation.current_used(&snapshot.spans[row.span]), None);
        assert_eq!(observation.watermark_used(), Some(0));
        assert!(observation.watermark_range().is_none());
        assert!(observation.watermark_headroom().unwrap() > 0);
    }

    #[test]
    fn checkpoints_do_not_carry_measurements_across_unknown_gaps_or_reused_stack_ids() {
        let mut app = app(measured_model());
        assert!(app.stack_rows().unwrap().1[0]
            .evidence(app.stack_rows().unwrap().0)
            .is_some());
        app.time_ns = Some(500_000_000);
        assert!(app.stack_rows().is_none());
        app.adjacent_snapshot(true);
        let (snapshot, rows) = app.stack_rows().unwrap();
        assert_eq!(snapshot.time_ns, 1_000_000_000);
        assert!(rows[0].evidence(snapshot).is_none());
        app.time_ns = None;
        assert!(app.stack_rows().is_none());
    }

    #[test]
    fn heap_replay_hides_coincident_stack_measurements() {
        let model = measured_model();
        let journal = serde_json::json!({
            "schema_version":1, "build":model.build,
            "provenance":{"nuttx_revision":"a".repeat(40),"config_sha256":"b".repeat(64),"collector":"stack-ui-test"},
            "recording_id":"stack-ui-test", "clock":"test-clock",
            "heap":{"id":"heap","epoch":"1","region":"demo-ram","start":0x2000_0800_u64,"size":256},
            "initial":{"sequence":0,"time_ns":0,"chunks":[{"start":0x2000_0800_u64,"size":256,"state":"free"}]},
            "records":[], "closing":{"heap_epoch":"1","sequence":0,"time_ns":0}
        });
        let replay =
            nxrs_memory_explorer::parse_heap_journal(&journal.to_string(), &model, None).unwrap();
        let mut app = app(model);
        app.replay = Some(replay);
        app.replay_enabled = true;
        assert!(app.model.as_ref().unwrap().snapshot_at(0).is_some());
        assert!(app.stack_rows().is_none());
        app.open_stack_memory("stack");
        assert_eq!(app.view, ViewMode::Stacks);
    }

    #[test]
    fn stack_address_navigation_uses_runtime_coordinates_even_after_alias_view() {
        let mut app = app(measured_model());
        let stack = app.stack_rows().unwrap().0.spans[app.stack_rows().unwrap().1[0].span].clone();
        app.switch_address_mode(true);
        app.open_stack_memory(&stack.id);
        assert_eq!(app.view, ViewMode::Memory);
        assert!(!app.physical);
        assert_eq!(app.selected.as_deref(), Some(stack.id.as_str()));
        assert_eq!(app.region, stack.region);
        assert!(app.viewport.start <= stack.start && stack.end() <= app.viewport.end);
    }

    #[test]
    fn stack_view_and_smoke_navigation_preserve_independent_heap_totals() {
        let mut app = app(measured_model());
        let before = app.model.as_ref().unwrap().occupied_bytes("demo-ram", 0);
        app.smoke_stack_navigation().unwrap();
        assert_eq!(app.view, ViewMode::Stacks);
        assert_eq!(app.time_ns, Some(0));
        assert_eq!(
            app.model.as_ref().unwrap().occupied_bytes("demo-ram", 0),
            before
        );
    }

    fn frame(
        app: &mut MemoryExplorer,
        ctx: &egui::Context,
        size: Vec2,
        events: Vec<egui::Event>,
    ) -> egui::FullOutput {
        ctx.run(
            egui::RawInput {
                screen_rect: Some(Rect::from_min_size(Pos2::ZERO, size)),
                events,
                ..Default::default()
            },
            |ctx| app.show(ctx),
        )
    }

    fn visible_text(output: &egui::FullOutput, text: &str) -> Pos2 {
        output
            .shapes
            .iter()
            .find_map(|clipped| {
                if let egui::epaint::Shape::Text(shape) = &clipped.shape {
                    let center = shape.pos + shape.galley.size() / 2.0;
                    if shape.galley.text() == text && clipped.clip_rect.contains(center) {
                        return Some(center);
                    }
                }
                None
            })
            .unwrap_or_else(|| panic!("Missing visible text: {text}"))
    }

    #[test]
    fn rendered_stack_controls_open_exact_memory_at_compact_and_large_sizes() {
        for size in [Vec2::new(1000.0, 700.0), Vec2::new(1600.0, 1100.0)] {
            let mut app = app(measured_model());
            let stack =
                app.stack_rows().unwrap().0.spans[app.stack_rows().unwrap().1[0].span].clone();
            let ctx = egui::Context::default();
            configure_style(&ctx);
            frame(&mut app, &ctx, size, vec![]);
            let output = frame(&mut app, &ctx, size, vec![]);
            visible_text(&output, "CURRENT DEPTH / HEADROOM");
            visible_text(&output, "TOUCHED / UNTOUCHED IN SCAN");
            let at = visible_text(&output, "Open in memory");
            for pressed in [true, false] {
                frame(
                    &mut app,
                    &ctx,
                    size,
                    vec![
                        egui::Event::PointerMoved(at),
                        egui::Event::PointerButton {
                            pos: at,
                            button: egui::PointerButton::Primary,
                            pressed,
                            modifiers: egui::Modifiers::NONE,
                        },
                    ],
                );
            }
            assert_eq!(app.view, ViewMode::Memory);
            assert_eq!(app.selected.as_deref(), Some(stack.id.as_str()));
            app.open_stack_usage(&stack);
            assert_eq!(app.view, ViewMode::Stacks);
            assert_eq!(app.stacks.selected, Some((0, stack.id)));
        }
    }

    #[test]
    fn task_table_only_renders_visible_rows_for_large_checkpoint_inventory() {
        let mut model = measured_model();
        model.regions.push(nxrs_memory_explorer::Region {
            id: "bulk-stacks".into(),
            name: "Synthetic stack inventory".into(),
            start: 0x4000_0000,
            size: 1024 * 1000,
            kind: RegionKind::Ram,
            domain: AddressDomain::CpuVirtual,
            physical: None,
        });
        let snapshot = &mut model.snapshots[0];
        for index in 0..1000 {
            snapshot.spans.push(Span {
                id: format!("bulk-{index}"),
                name: format!("Bulk stack {index}"),
                region: "bulk-stacks".into(),
                start: 0x4000_0000 + index * 1024,
                size: 1024,
                kind: SpanKind::Stack,
                source: None,
                parent: None,
                detail: None,
                backtrace: vec![],
            });
        }
        model.validate().unwrap();
        let mut app = app(model);
        assert_eq!(app.stack_rows().unwrap().1.len(), 1001);
        let ctx = egui::Context::default();
        configure_style(&ctx);
        frame(&mut app, &ctx, Vec2::new(1000.0, 700.0), vec![]);
        let output = frame(&mut app, &ctx, Vec2::new(1000.0, 700.0), vec![]);
        let rendered = output.shapes.iter().filter(|clipped| matches!(&clipped.shape, egui::epaint::Shape::Text(shape) if shape.galley.text().starts_with("Bulk stack"))).count();
        assert!(
            rendered > 0 && rendered < 20,
            "Rendered {rendered} of 1000 table rows"
        );
    }
}
