use super::*;
use nxrs_memory_explorer::{CounterSample, TimingEvent, TimingEventKind, TimingRange, TimingTrace};

#[derive(Default)]
pub(super) struct TimingView {
    viewport: Option<AddressWindow>,
    selected: Option<usize>,
    cursor_ns: Option<u64>,
    selection: Option<AddressWindow>,
    measure_anchor: Option<u64>,
    measure_mode: bool,
    filter: String,
    show_counters: bool,
}

pub(super) fn bounds(trace: &TimingTrace) -> AddressWindow {
    let start = trace.bounds.start_ns;
    let end = trace.bounds.end_ns;
    if end > start {
        AddressWindow { start, end }
    } else if start > 0 {
        AddressWindow {
            start: start - 1,
            end,
        }
    } else {
        AddressWindow { start: 0, end: 1 }
    }
}

fn range(window: AddressWindow) -> TimingRange {
    TimingRange {
        start_ns: window.start,
        end_ns: window.end,
    }
}

fn elapsed(ns: u64) -> String {
    if ns < 1_000 {
        format!("{ns} ns")
    } else if ns < 1_000_000 {
        format!("{:.3} us", ns as f64 / 1_000.0)
    } else if ns < 1_000_000_000 {
        format!("{:.3} ms", ns as f64 / 1_000_000.0)
    } else {
        format!("{:.6} s", ns as f64 / 1_000_000_000.0)
    }
}

fn event_color(event: &TimingEvent) -> Color32 {
    if event.category.contains("irq") || event.category.contains("interrupt") {
        Color32::from_rgb(230, 167, 94)
    } else if event.kind == TimingEventKind::Instant {
        WARNING
    } else {
        source_color(&event.name)
    }
}

impl TimingView {
    pub(super) fn cursor(&self) -> Option<u64> {
        self.cursor_ns
    }

    pub(super) fn clear_cursor(&mut self) {
        self.cursor_ns = None;
        self.selected = None;
    }

    pub(super) fn seek_checkpoint(
        &mut self,
        link: &nxrs_memory_explorer::CheckpointLink,
        trace: &TimingTrace,
    ) {
        self.seek_time(link.trace_time_ns, trace);
        self.selected = Some(link.event_id);
    }

    pub(super) fn seek_time(&mut self, time_ns: u64, trace: &TimingTrace) {
        let full = bounds(trace);
        let time_ns = time_ns.clamp(trace.bounds.start_ns, trace.bounds.end_ns);
        self.cursor_ns = Some(time_ns);
        self.selected = None;
        let viewport = self.viewport.unwrap_or(full);
        if time_ns < viewport.start || time_ns > viewport.end {
            let start = time_ns.saturating_sub(viewport.span() / 2);
            self.viewport = Some(
                AddressWindow {
                    start,
                    end: start.saturating_add(viewport.span()),
                }
                .fit_inside(full),
            );
        }
    }

    pub(super) fn reset(&mut self, trace: &TimingTrace) {
        *self = Self {
            viewport: Some(bounds(trace)),
            show_counters: true,
            ..Self::default()
        };
    }

    fn focus(&mut self, event: &TimingEvent, trace: &TimingTrace) {
        let extent = event.end_ns.saturating_sub(event.start_ns).max(1);
        let padding = (extent / 4).max(1);
        self.viewport = Some(
            AddressWindow {
                start: event.start_ns.saturating_sub(padding),
                end: event.end_ns.saturating_add(padding),
            }
            .fit_inside(bounds(trace)),
        );
        self.selected = Some(event.id);
        self.cursor_ns = Some(event.start_ns);
    }

    pub(super) fn show(&mut self, ctx: &egui::Context, trace: &TimingTrace) {
        self.inspector(ctx, trace);
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(BACKGROUND).inner_margin(20))
            .show(ctx, |ui| {
                let full = bounds(trace);
                let mut viewport = self.viewport.unwrap_or(full);
                ui.horizontal(|ui| {
                    ui.heading("Execution timeline");
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.button("Fit trace").clicked() { viewport = full; }
                        if ui.button("+").clicked() { viewport = viewport.zoom(0.5, viewport.at_fraction(0.5), full); }
                        if ui.button("-").clicked() { viewport = viewport.zoom(2.0, viewport.at_fraction(0.5), full); }
                    });
                });
                ui.label(RichText::new(format!("Clock: {}", trace.metadata.clock.as_deref().unwrap_or("unspecified by producer"))).small().color(MUTED));
                if let Some(source) = &trace.metadata.source {
                    ui.label(RichText::new(source).small().color(MUTED));
                }
                ui.label(RichText::new(format!("Axis shows elapsed time from the first record at {} ns; the inspector retains trace-file coordinates.", trace.bounds.start_ns)).small().color(MUTED));
                ui.horizontal_wrapped(|ui| {
                    if trace.metadata.loss_detected == Some(true) || trace.metadata.lost_records.is_some_and(|count| count > 0) {
                        badge(ui, "RECORD LOSS REPORTED", WARNING);
                    } else if trace.metadata.loss_detected == Some(false) || trace.metadata.lost_records == Some(0) {
                        badge(ui, "NO LOSS REPORTED", ACCENT);
                    } else {
                        badge(ui, "LOSS STATUS UNKNOWN", WARNING);
                    }
                    if trace.metadata.complete != Some(true) || trace.metadata.incomplete_intervals > 0 {
                        badge(ui, "PARTIAL OR UNVERIFIED COVERAGE", WARNING);
                    }
                    ui.label(RichText::new("Observed intervals only; blank time is not inferred idle.").small().color(MUTED));
                });
                ui.add_space(10.0);
                let measured = self.selection.unwrap_or(viewport);
                let stats = trace.stats(range(measured), &self.filter);
                ui.columns(3, |columns| {
                    for (column, (label, value, detail)) in columns.iter_mut().zip([
                        ("VISIBLE WINDOW", elapsed(viewport.span()), format!("{} .. {} ns", viewport.start, viewport.end)),
                        (if self.selection.is_some() { "MEASURED SELECTION" } else { "OBSERVED INTERVALS" }, if self.selection.is_some() { elapsed(measured.span()) } else { stats.interval_count.to_string() }, if self.selection.is_some() { format!("{} ns; endpoint subtraction", measured.span()) } else { "Matching the visible window".into() }),
                        ("TRACE RECORDS", format!("{} / {}", trace.events.len(), trace.counters.len()), "Intervals + instants / counter samples".into()),
                    ]) {
                        egui::Frame::new().fill(SURFACE).corner_radius(7).inner_margin(11).show(column, |ui| {
                            ui.set_min_width(ui.available_width());
                            ui.label(RichText::new(label).size(9.0).color(MUTED));
                            ui.label(RichText::new(value).size(21.0).strong());
                            ui.label(RichText::new(detail).size(10.0).color(MUTED));
                        });
                    }
                });
                ui.add_space(12.0);
                ui.horizontal_wrapped(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut self.filter).hint_text("Filter task, IRQ, span or category").desired_width(280.0));
                    ui.checkbox(&mut self.show_counters, "Counters");
                    ui.checkbox(&mut self.measure_mode, "Measure range").on_hover_text("Drag across time to measure. Clear this to pan, or hold Shift for a temporary measurement.");
                    if !self.filter.is_empty() && ui.small_button("Clear filter").clicked() { self.filter.clear(); }
                    if self.selection.is_some() && ui.small_button("Clear measurement").clicked() { self.selection = None; }
                });
                ui.label(RichText::new("Click to place cursor · scroll to zoom · drag to pan · Shift+drag to measure · double-click a span to fit · F to fit trace").small().color(MUTED));
                if !ctx.wants_keyboard_input() {
                    ctx.input(|input| {
                        if input.key_pressed(egui::Key::F) || input.key_pressed(egui::Key::Home) { viewport = full; }
                        if input.key_pressed(egui::Key::Plus) || input.key_pressed(egui::Key::Equals) { viewport = viewport.zoom(0.5, viewport.at_fraction(0.5), full); }
                        if input.key_pressed(egui::Key::Minus) { viewport = viewport.zoom(2.0, viewport.at_fraction(0.5), full); }
                        if input.key_pressed(egui::Key::ArrowLeft) { viewport = viewport.pan(-0.15, full); }
                        if input.key_pressed(egui::Key::ArrowRight) { viewport = viewport.pan(0.15, full); }
                    });
                }
                self.viewport = Some(viewport);
                ui.add_space(10.0);
                egui::ScrollArea::vertical().id_salt("timing-track-scroll").auto_shrink([false, false]).show(ui, |ui| {
                    self.canvas(ui, trace);
                    if !trace.warnings.is_empty() {
                        ui.add_space(12.0);
                        egui::CollapsingHeader::new(format!("Capture notes ({})", trace.warnings.len())).show(ui, |ui| {
                            for warning in &trace.warnings { ui.label(RichText::new(warning).small().color(WARNING)); }
                        });
                    }
                });
            });
    }

    fn canvas(&mut self, ui: &mut egui::Ui, trace: &TimingTrace) {
        let full = bounds(trace);
        let mut viewport = self.viewport.unwrap_or(full);
        let tracks: Vec<_> = trace
            .tracks
            .iter()
            .filter(|track| {
                trace.events.iter().any(|event| {
                    event.track == track.id && trace.event_matches(event, &self.filter)
                })
            })
            .collect();
        let heights: Vec<_> = tracks
            .iter()
            .map(|track| {
                let depth = trace
                    .events
                    .iter()
                    .filter(|event| {
                        event.track == track.id && trace.event_matches(event, &self.filter)
                    })
                    .map(|event| event.depth)
                    .max()
                    .unwrap_or(0);
                32.0 + (depth + 1) as f32 * 27.0
            })
            .collect();
        let mut counters: BTreeMap<(usize, String, String), Vec<&CounterSample>> = BTreeMap::new();
        if self.show_counters {
            for sample in &trace.counters {
                let track = &trace.tracks[sample.track];
                if self.filter.is_empty()
                    || format!(
                        "{} {} {} {}",
                        sample.name, sample.series, track.name, track.process_name
                    )
                    .to_lowercase()
                    .contains(&self.filter.to_lowercase())
                {
                    counters
                        .entry((sample.track, sample.name.clone(), sample.series.clone()))
                        .or_default()
                        .push(sample);
                }
            }
        }
        let total_height = 42.0 + heights.iter().sum::<f32>() + counters.len() as f32 * 88.0 + 12.0;
        let (rect, response) = ui.allocate_exact_size(
            Vec2::new(ui.available_width().max(360.0), total_height.max(150.0)),
            Sense::click_and_drag(),
        );
        let plot = Rect::from_min_max(
            Pos2::new(rect.left() + 178.0, rect.top() + 38.0),
            Pos2::new(rect.right() - 12.0, rect.bottom() - 12.0),
        );
        if response
            .hover_pos()
            .is_some_and(|pointer| pointer.x >= plot.left())
        {
            let scroll = ui.input_mut(|input| std::mem::take(&mut input.smooth_scroll_delta));
            if let Some(pointer) = response.hover_pos().filter(|_| scroll.y != 0.0) {
                let anchor =
                    viewport.at_fraction(((pointer.x - plot.left()) / plot.width()) as f64);
                viewport = viewport.zoom((-scroll.y as f64 * 0.004).exp(), anchor, full);
            }
            if scroll.x != 0.0 {
                viewport = viewport.pan(-scroll.x as f64 / plot.width() as f64, full);
            }
        }
        if response.drag_started() {
            self.measure_anchor = ui
                .input(|input| {
                    (self.measure_mode || input.modifiers.shift)
                        .then(|| input.pointer.press_origin())
                        .flatten()
                })
                .filter(|pointer| pointer.x >= plot.left() && pointer.x <= plot.right())
                .map(|pointer| {
                    viewport.at_fraction(((pointer.x - plot.left()) / plot.width()) as f64)
                });
            if self.measure_anchor.is_some() {
                self.selection = None;
            }
        }
        if response.dragged() {
            if let Some(anchor) = self.measure_anchor {
                if let Some(pointer) = response.interact_pointer_pos() {
                    let current =
                        viewport.at_fraction(((pointer.x - plot.left()) / plot.width()) as f64);
                    self.selection = (current != anchor).then_some(AddressWindow {
                        start: current.min(anchor),
                        end: current.max(anchor),
                    });
                }
            } else {
                viewport = viewport.pan(
                    -ui.input(|input| input.pointer.delta().x) as f64 / plot.width() as f64,
                    full,
                );
            }
        }
        if response.drag_stopped() {
            self.measure_anchor = None;
        }
        self.viewport = Some(viewport);
        let painter = ui.painter().with_clip_rect(rect);
        painter.rect_filled(rect, 8, PANEL);
        painter.rect_stroke(rect, 8, Stroke::new(1.0, LINE), StrokeKind::Inside);
        let at = |time| plot.left() + viewport.fraction(time) as f32 * plot.width();
        let ticks = ((plot.width() / 110.0) as u64).max(1).min(viewport.span());
        for index in 0..=ticks {
            let time =
                viewport.start + ((viewport.span() as u128 * index as u128) / ticks as u128) as u64;
            let x = at(time);
            painter.line_segment(
                [Pos2::new(x, plot.top()), Pos2::new(x, plot.bottom())],
                Stroke::new(1.0, LINE),
            );
        }
        let mut top = plot.top();
        let mut hit: Option<&TimingEvent> = None;
        let visible = trace.query(range(viewport), &self.filter);
        for (track, height) in tracks.iter().zip(&heights) {
            painter.rect_filled(
                Rect::from_min_size(Pos2::new(rect.left() + 1.0, top), Vec2::new(171.0, *height)),
                0,
                PANEL,
            );
            painter.text(
                Pos2::new(rect.left() + 12.0, top + 12.0),
                Align2::LEFT_TOP,
                abbreviated(&track.name, 25),
                FontId::proportional(12.0),
                TEXT,
            );
            painter.text(
                Pos2::new(rect.left() + 12.0, top + 31.0),
                Align2::LEFT_TOP,
                abbreviated(&track.process_name, 28),
                FontId::proportional(9.0),
                MUTED,
            );
            painter.line_segment(
                [
                    Pos2::new(rect.left() + 8.0, top + height),
                    Pos2::new(plot.right(), top + height),
                ],
                Stroke::new(1.0, LINE),
            );
            for event in visible.iter().filter(|event| event.track == track.id) {
                let y = top + 16.0 + event.depth as f32 * 27.0;
                let x0 = at(event.start_ns).max(plot.left());
                let x1 = at(event.end_ns).min(plot.right());
                let block = Rect::from_min_max(
                    Pos2::new(x0, y),
                    Pos2::new(x1.max(x0 + 1.0).min(plot.right()), y + 23.0),
                );
                let color = event_color(event);
                if event.kind == TimingEventKind::Instant {
                    if (plot.left()..=plot.right()).contains(&x0) {
                        painter.line_segment(
                            [Pos2::new(x0, y), Pos2::new(x0, y + 23.0)],
                            Stroke::new(2.0, color),
                        );
                        painter.circle_filled(Pos2::new(x0, y + 2.0), 3.0, color);
                    }
                } else {
                    painter.rect_filled(block, 3, color.linear_multiply(0.72));
                    if self.selected == Some(event.id) {
                        painter.rect_stroke(
                            block,
                            3,
                            Stroke::new(2.0, Color32::WHITE),
                            StrokeKind::Inside,
                        );
                    }
                    if block.width() > 48.0 {
                        painter.with_clip_rect(block.shrink(3.0)).text(
                            Pos2::new(block.left() + 6.0, block.center().y),
                            Align2::LEFT_CENTER,
                            &event.name,
                            FontId::proportional(11.0),
                            TEXT,
                        );
                    }
                }
                if response
                    .hover_pos()
                    .is_some_and(|pointer| block.expand2(Vec2::new(3.0, 0.0)).contains(pointer))
                {
                    hit = Some(event);
                }
            }
            top += height;
        }
        let mut sample_hit: Option<&CounterSample> = None;
        for ((track_id, name, series), samples) in counters {
            let visible: Vec<_> = samples
                .into_iter()
                .filter(|sample| sample.time_ns >= viewport.start && sample.time_ns <= viewport.end)
                .collect();
            let low = visible
                .iter()
                .map(|sample| sample.value)
                .reduce(f64::min)
                .unwrap_or(0.0);
            let high = visible
                .iter()
                .map(|sample| sample.value)
                .reduce(f64::max)
                .unwrap_or(0.0);
            painter.rect_filled(
                Rect::from_min_size(Pos2::new(rect.left() + 1.0, top), Vec2::new(171.0, 88.0)),
                0,
                PANEL,
            );
            painter.text(
                Pos2::new(rect.left() + 12.0, top + 12.0),
                Align2::LEFT_TOP,
                abbreviated(&format!("{name}: {series}"), 25),
                FontId::proportional(11.0),
                TEXT,
            );
            painter.text(
                Pos2::new(rect.left() + 12.0, top + 31.0),
                Align2::LEFT_TOP,
                format!("{low:.2} .. {high:.2}"),
                FontId::monospace(9.0),
                MUTED,
            );
            painter.text(
                Pos2::new(rect.left() + 12.0, top + 46.0),
                Align2::LEFT_TOP,
                abbreviated(&trace.tracks[track_id].name, 25),
                FontId::proportional(9.0),
                MUTED,
            );
            painter.line_segment(
                [
                    Pos2::new(plot.left(), top + 72.0),
                    Pos2::new(plot.right(), top + 72.0),
                ],
                Stroke::new(1.0, LINE),
            );
            for sample in visible {
                // Scale before subtracting so two finite extreme values cannot
                // overflow the plotted range into infinity or NaN.
                let scale = low.abs().max(high.abs()).max(1.0);
                let fraction = if high > low {
                    (sample.value / scale - low / scale) / (high / scale - low / scale)
                } else {
                    0.5
                };
                let point = Pos2::new(at(sample.time_ns), top + 68.0 - fraction as f32 * 48.0);
                painter.line_segment(
                    [Pos2::new(point.x, top + 72.0), point],
                    Stroke::new(1.0, ACCENT.linear_multiply(0.35)),
                );
                painter.circle_filled(point, 3.0, ACCENT);
                if response
                    .hover_pos()
                    .is_some_and(|pointer| pointer.distance(point) < 8.0)
                {
                    sample_hit = Some(sample);
                }
            }
            top += 88.0;
        }
        if let Some(selection) = self.selection {
            let a = at(selection.start).clamp(plot.left(), plot.right());
            let b = at(selection.end).clamp(plot.left(), plot.right());
            if b > a {
                let band =
                    Rect::from_min_max(Pos2::new(a, plot.top()), Pos2::new(b, plot.bottom()));
                painter.rect_filled(band, 0, Color32::from_rgba_unmultiplied(82, 215, 191, 24));
                for x in [a, b] {
                    painter.line_segment(
                        [Pos2::new(x, plot.top()), Pos2::new(x, plot.bottom())],
                        Stroke::new(1.0, ACCENT),
                    );
                }
            }
        }
        if let Some(cursor) = self
            .cursor_ns
            .filter(|time| *time >= viewport.start && *time <= viewport.end)
        {
            let x = at(cursor);
            painter.line_segment(
                [Pos2::new(x, plot.top()), Pos2::new(x, plot.bottom())],
                Stroke::new(1.0, ACCENT),
            );
        }
        // Keep the time scale visible while the user scrolls through tracks.
        // Paint it last so scrolled intervals cannot cover its labels.
        let ruler_top = rect.top().max(ui.clip_rect().top());
        let ruler = Rect::from_min_size(
            Pos2::new(rect.left(), ruler_top),
            Vec2::new(rect.width(), 38.0),
        );
        painter.rect_filled(ruler, 0, PANEL);
        painter.line_segment(
            [ruler.left_bottom(), ruler.right_bottom()],
            Stroke::new(1.0, LINE),
        );
        painter.text(
            Pos2::new(rect.left() + 12.0, ruler.center().y),
            Align2::LEFT_CENTER,
            "TRACK / ELAPSED TIME",
            FontId::proportional(9.0),
            MUTED,
        );
        for index in 0..=ticks {
            let time =
                viewport.start + ((viewport.span() as u128 * index as u128) / ticks as u128) as u64;
            let align = if index == 0 {
                Align2::LEFT_CENTER
            } else if index == ticks {
                Align2::RIGHT_CENTER
            } else {
                Align2::CENTER_CENTER
            };
            painter.text(
                Pos2::new(at(time), ruler.center().y),
                align,
                elapsed(time.saturating_sub(trace.bounds.start_ns)),
                FontId::monospace(10.0),
                MUTED,
            );
        }
        if response
            .hover_pos()
            .is_some_and(|pointer| pointer.y <= ruler.bottom())
        {
            hit = None;
            sample_hit = None;
        }
        if let Some(event) = hit {
            response.clone().on_hover_ui_at_pointer(|ui| {
                ui.strong(&event.name);
                ui.label(format!("{} ns .. {} ns", event.start_ns, event.end_ns));
                ui.label(format!(
                    "Duration: {} ({} ns)",
                    elapsed(event.duration_ns()),
                    event.duration_ns()
                ));
                ui.label(&event.category);
            });
            if response.clicked() {
                self.selected = Some(event.id);
                self.cursor_ns = Some(event.start_ns);
            }
            if response.double_clicked() {
                self.focus(event, trace);
            }
        } else if let Some(sample) = sample_hit {
            if response.clicked() {
                self.cursor_ns = Some(sample.time_ns);
                self.selected = None;
            }
            response.on_hover_ui_at_pointer(|ui| {
                ui.strong(format!("{} / {}", sample.name, sample.series));
                ui.label(format!("{} ns: {}", sample.time_ns, sample.exact_value));
                ui.label("Recorded sample; intermediate values are not inferred.");
            });
        } else if response.clicked() {
            if let Some(pointer) = response
                .interact_pointer_pos()
                .filter(|pointer| pointer.x >= plot.left() && pointer.x <= plot.right())
            {
                self.cursor_ns =
                    Some(viewport.at_fraction(((pointer.x - plot.left()) / plot.width()) as f64));
                self.selected = None;
            }
        }
    }

    fn inspector(&mut self, ctx: &egui::Context, trace: &TimingTrace) {
        egui::SidePanel::right("timing-inspector").default_width(285.0).width_range(240.0..=440.0)
            .frame(egui::Frame::new().fill(PANEL).inner_margin(18))
            .show(ctx, |ui| {
                egui::ScrollArea::vertical().id_salt("timing-inspector-scroll").show(ui, |ui| {
                    eyebrow(ui, "TIMING INSPECTOR");
                    ui.add_space(14.0);
                    if let Some(event) = self.selected.and_then(|id| trace.events.iter().find(|event| event.id == id)) {
                        badge(ui, if event.kind == TimingEventKind::Instant { "INSTANT" } else { "OBSERVED INTERVAL" }, event_color(event));
                        ui.add_space(10.0);
                        ui.heading(&event.name);
                        ui.add_space(14.0);
                        field(ui, "DURATION", &format!("{}  ({} ns)", elapsed(event.duration_ns()), event.duration_ns()));
                        field(ui, "START · TRACE COORDINATE", &format!("{} ns", event.start_ns));
                        field(ui, "END · TRACE COORDINATE", &format!("{} ns", event.end_ns));
                        if let Some(origin) = trace.metadata.origin_ns {
                            if let (Some(start), Some(end)) = (origin.checked_add(event.start_ns), origin.checked_add(event.end_ns)) {
                                field(ui, "SOURCE CLOCK · START / END", &format!("{start} .. {end} ns"));
                            } else {
                                ui.label(RichText::new("Source-clock origin plus trace coordinate exceeds the supported range.").small().color(WARNING));
                            }
                        }
                        field(ui, "TRACK", &trace.tracks[event.track].name);
                        field(ui, "CATEGORY", &event.category);
                        if ui.button("Fit this interval").clicked() { self.focus(event, trace); }
                        if event.end_ns > event.start_ns && ui.button("Measure this interval").clicked() {
                            self.selection = Some(AddressWindow { start: event.start_ns, end: event.end_ns });
                        }
                        if event.args.as_object().is_some_and(|args| !args.is_empty()) {
                            ui.add_space(10.0);
                            eyebrow(ui, "RECORDED ARGUMENTS");
                            ui.label(RichText::new(serde_json::to_string_pretty(&event.args).unwrap_or_default()).monospace().size(10.0).color(MUTED));
                        }
                    } else {
                        ui.heading("Inspect an interval");
                        ui.label(RichText::new("Click a task, interrupt or named span. Shift+drag across time to measure an arbitrary range.").color(MUTED));
                    }
                    ui.add_space(18.0);
                    ui.separator();
                    let selected = self.selection.unwrap_or_else(|| self.viewport.unwrap_or_else(|| bounds(trace)));
                    let stats = trace.stats(range(selected), &self.filter);
                    eyebrow(ui, if self.selection.is_some() { "SELECTION COVERAGE" } else { "VISIBLE COVERAGE" });
                    ui.label(RichText::new("Union of observed intervals per track. Nested intervals are counted once within a track. Tracks may describe the same time; their totals are not additive CPU utilization.").small().color(MUTED));
                    for track in stats.tracks.iter().filter(|track| track.interval_count > 0) {
                        ui.add_space(7.0);
                        ui.label(&trace.tracks[track.track].name);
                        ui.label(RichText::new(format!("{} · {} intervals", elapsed(track.covered_ns), track.interval_count)).small().color(ACCENT));
                    }
                    ui.add_space(16.0);
                    ui.separator();
                    eyebrow(ui, "CLOCK AND COVERAGE");
                    if let Some(cursor) = self.cursor_ns {
                        field(ui, "CURSOR · TRACE COORDINATE", &format!("{cursor} ns"));
                    }
                    field(ui, "CLOCK", trace.metadata.clock.as_deref().unwrap_or("Unknown"));
                    if let Some(recording) = &trace.metadata.recording_id {
                        field(ui, "RECORDING", recording);
                    }
                    if let Some(origin) = trace.metadata.origin_ns { field(ui, "RECORDED ORIGIN", &format!("{origin} ns")); }
                    field(ui, "ELF SHA-256", trace.metadata.elf_sha256.as_deref().unwrap_or("Unknown; build identity is not established"));
                    ui.label(RichText::new("Nanosecond coordinates describe the file's timestamps, not a guarantee of nanosecond clock resolution. QEMU durations do not qualify physical MCU timing.").small().color(WARNING));
                    if trace.metadata.incomplete_intervals > 0 {
                        ui.label(RichText::new(format!("{} incomplete interval boundaries are not assigned durations.", trace.metadata.incomplete_intervals)).small().color(WARNING));
                    }
                });
            });
    }

    pub(super) fn smoke(&mut self, trace: &TimingTrace) -> Result<(), String> {
        let full = bounds(trace);
        let zoom = full.zoom(0.5, full.at_fraction(0.5), full);
        let pan = zoom.pan(0.1, full);
        if pan.start < full.start || pan.end > full.end || zoom.span() > full.span() {
            return Err("Timing navigation escaped trace bounds".into());
        }
        let event = trace
            .events
            .iter()
            .find(|event| event.kind == TimingEventKind::Interval && event.end_ns > event.start_ns)
            .ok_or("Timing smoke needs an observed interval")?;
        self.selected = Some(event.id);
        self.cursor_ns = Some(event.start_ns);
        self.selection = Some(AddressWindow {
            start: event.start_ns,
            end: event.end_ns,
        });
        let stats = trace.stats(
            TimingRange {
                start_ns: event.start_ns,
                end_ns: event.end_ns,
            },
            "",
        );
        if stats.interval_count == 0 || event.duration_ns() != event.end_ns - event.start_ns {
            return Err("Timing selection did not retain exact duration".into());
        }
        self.viewport = Some(full);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timing_navigation_keeps_adjacent_high_timestamps_distinct() {
        let start = (1u64 << 61) + 9;
        let view = AddressWindow {
            start,
            end: start + 256,
        };
        assert_eq!(view.at_fraction(1.0 / 256.0), start + 1);
        assert_eq!(view.fraction(start + 1), 1.0 / 256.0);
    }

    #[test]
    fn timing_smoke_selects_observed_duration_without_summing_tracks() {
        let trace = nxrs_memory_explorer::timing_demo();
        let mut view = TimingView::default();
        view.reset(&trace);
        view.smoke(&trace).unwrap();
        let selected = view.selection.unwrap();
        let event = trace
            .events
            .iter()
            .find(|event| Some(event.id) == view.selected)
            .unwrap();
        assert_eq!(selected.span(), event.duration_ns());
        assert_eq!(view.viewport, Some(bounds(&trace)));
    }
}
