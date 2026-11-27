use super::*;
use nxrs_memory_explorer::{
    CounterSample, TimingEvent, TimingEventKind, TimingRange, TimingSourceStats, TimingTrace,
};

#[derive(Default, PartialEq, Eq)]
enum TimingMode {
    #[default]
    Timeline,
    Events,
    Sources,
}

struct TimingAnalysis {
    range: TimingRange,
    filter: String,
    minimum_ns: u64,
    events: Vec<usize>,
    sources: Vec<TimingSourceStats>,
    stats: nxrs_memory_explorer::TimingStats,
}

struct TrackLayout {
    id: usize,
    depth: u32,
    events: Vec<usize>,
    prefix_end: Vec<u64>,
}

impl TrackLayout {
    fn visible<'a, 't: 'a>(
        &'a self,
        trace: &'t TimingTrace,
        window: TimingRange,
    ) -> impl Iterator<Item = &'t TimingEvent> + 'a {
        let begin = self
            .prefix_end
            .partition_point(|end| *end < window.start_ns);
        let end = self
            .events
            .partition_point(|index| trace.events[*index].start_ns <= window.end_ns);
        self.events[begin.min(end)..end]
            .iter()
            .map(move |index| &trace.events[*index])
            .filter(move |event| event.intersects(window))
    }
}

#[derive(Default)]
struct TimingLayout {
    filter: String,
    minimum_ns: u64,
    tracks: Vec<TrackLayout>,
    counters: BTreeMap<(usize, String, String), Vec<usize>>,
}

#[derive(Default)]
struct SourceCoverage {
    irqs: usize,
    irq_sources: usize,
    heap: usize,
    heap_sources: usize,
    app: usize,
    timers: usize,
}

impl SourceCoverage {
    fn build(trace: &TimingTrace) -> Self {
        let mut result = Self::default();
        for event in &trace.events {
            if event.is_irq() {
                result.irqs += 1;
                result.irq_sources += usize::from(event.source_name().is_some());
            }
            if event.is_heap() {
                result.heap += 1;
                result.heap_sources += usize::from(event.source_name().is_some());
            }
            if event.category.split(',').any(|part| part.trim() == "app") {
                result.app += 1;
            }
            if event.category.split(',').any(|part| part.trim() == "timer") {
                result.timers += 1;
            }
        }
        result
    }
}

impl TimingLayout {
    fn build(trace: &TimingTrace, filter: &str, minimum_ns: u64) -> Self {
        let mut by_track: BTreeMap<usize, TrackLayout> = BTreeMap::new();
        for (index, event) in trace.events.iter().enumerate() {
            if !trace.event_matches(event, filter)
                || (event.kind == TimingEventKind::Interval && event.duration_ns() < minimum_ns)
            {
                continue;
            }
            let track = by_track.entry(event.track).or_insert_with(|| TrackLayout {
                id: event.track,
                depth: 0,
                events: Vec::new(),
                prefix_end: Vec::new(),
            });
            track.depth = track.depth.max(event.depth);
            track.events.push(index);
            track.prefix_end.push(
                track
                    .prefix_end
                    .last()
                    .copied()
                    .unwrap_or(0)
                    .max(event.end_ns),
            );
        }
        let mut counters: BTreeMap<_, Vec<usize>> = BTreeMap::new();
        for (index, sample) in trace.counters.iter().enumerate() {
            if trace.counter_matches(sample, filter) {
                counters
                    .entry((sample.track, sample.name.clone(), sample.series.clone()))
                    .or_default()
                    .push(index);
            }
        }
        Self {
            filter: filter.to_owned(),
            minimum_ns,
            tracks: by_track.into_values().collect(),
            counters,
        }
    }
}

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
    mode: TimingMode,
    minimum_text: String,
    minimum_ns: u64,
    selected_counter: Option<usize>,
    analysis: Option<TimingAnalysis>,
    layout: Option<TimingLayout>,
    source_coverage: Option<SourceCoverage>,
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

fn elapsed_u128(ns: u128) -> String {
    u64::try_from(ns)
        .map(elapsed)
        .unwrap_or_else(|_| format!("{ns} ns"))
}

fn request_outcome(event: &TimingEvent) -> &'static str {
    match event.request_failed() {
        Some(true) => "failed",
        Some(false) => "succeeded",
        None => "outcome not captured",
    }
}

fn recorded_text(event: &TimingEvent, names: &[&str]) -> Option<String> {
    names
        .iter()
        .find_map(|name| event.argument(name))
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| value.to_string())
        })
}

fn unrecorded_source(category: &str) -> &'static str {
    if category
        .split(',')
        .any(|part| matches!(part.trim(), "irq" | "interrupt" | "timer"))
    {
        "Handler not recorded or unresolved"
    } else if category.split(',').any(|part| part.trim() == "heap") {
        "Caller not recorded or unresolved"
    } else if category.split(',').any(|part| part.trim() == "app") {
        "Named application observation; no callsite recorded"
    } else if category.split(',').any(|part| part.trim() == "sched") {
        "Scheduler observation"
    } else {
        "Source not recorded"
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
    pub(super) fn set_analysis_mode(&mut self, mode: &str) {
        self.mode = match mode {
            "events" => TimingMode::Events,
            "sources" => TimingMode::Sources,
            _ => TimingMode::Timeline,
        };
    }

    pub(super) fn cursor(&self) -> Option<u64> {
        self.cursor_ns
    }

    pub(super) fn clear_cursor(&mut self) {
        self.cursor_ns = None;
        self.selected = None;
        self.selected_counter = None;
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
        self.selected_counter = None;
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
            minimum_text: "0".into(),
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
        self.selected_counter = None;
        self.cursor_ns = Some(event.start_ns);
    }

    fn refresh_analysis(&mut self, trace: &TimingTrace, selected: TimingRange) {
        if self.analysis.as_ref().is_some_and(|analysis| {
            analysis.range == selected
                && analysis.filter == self.filter
                && analysis.minimum_ns == self.minimum_ns
        }) {
            return;
        }
        let events = trace
            .events
            .iter()
            .enumerate()
            .filter(|(_, event)| {
                event.intersects(selected)
                    && trace.event_matches(event, &self.filter)
                    && (event.kind == TimingEventKind::Instant
                        || event.duration_ns() >= self.minimum_ns)
            })
            .map(|(index, _)| index)
            .collect();
        self.analysis = Some(TimingAnalysis {
            range: selected,
            filter: self.filter.clone(),
            minimum_ns: self.minimum_ns,
            events,
            sources: trace.source_stats(selected, &self.filter, self.minimum_ns),
            stats: trace.stats_filtered(selected, &self.filter, self.minimum_ns),
        });
    }

    fn event_table(&mut self, ui: &mut egui::Ui, trace: &TimingTrace) {
        let selected = self
            .selection
            .unwrap_or_else(|| self.viewport.unwrap_or_else(|| bounds(trace)));
        self.refresh_analysis(trace, range(selected));
        let analysis = self.analysis.as_ref().unwrap();
        ui.label(RichText::new(format!("{} matching events · chronological order · click to inspect · double-click to locate on timeline", analysis.events.len())).small().color(MUTED));
        if analysis.events.is_empty() {
            ui.label("No events match this time window and filter.");
        }
        let mut chosen = None;
        egui::ScrollArea::vertical()
            .id_salt("timing-event-list")
            .auto_shrink([false, false])
            .show_rows(ui, 58.0, analysis.events.len(), |ui, rows| {
                for row in rows {
                    let event = &trace.events[analysis.events[row]];
                    let source = event
                        .source_name()
                        .unwrap_or_else(|| unrecorded_source(&event.category));
                    let duration = if event.kind == TimingEventKind::Instant {
                        "instant".into()
                    } else {
                        elapsed(event.duration_ns())
                    };
                    let detail = if let Some(bytes) = event.requested_bytes() {
                        format!(" · request {} B · {}", bytes, request_outcome(event))
                    } else {
                        String::new()
                    };
                    let first_line = format!(
                        "{}  ·  {}  ·  {}{}",
                        elapsed(event.start_ns.saturating_sub(trace.bounds.start_ns)),
                        duration,
                        event.name,
                        detail
                    );
                    let second_line = format!(
                        "{}  ·  {}  ·  {}",
                        trace.tracks[event.track].name, event.category, source
                    );
                    let line_width = ((ui.available_width() - 24.0) / 7.0).max(30.0) as usize;
                    let text = format!(
                        "{}\n{}",
                        abbreviated(&first_line, line_width),
                        abbreviated(&second_line, line_width)
                    );
                    let size = Vec2::new(ui.available_width(), 54.0);
                    let response = ui
                        .allocate_ui_with_layout(
                            size,
                            egui::Layout::left_to_right(egui::Align::Center)
                                .with_main_align(egui::Align::Min),
                            |ui| {
                                ui.add(
                                    egui::Button::selectable(
                                        self.selected == Some(event.id),
                                        RichText::new(text).size(11.0),
                                    )
                                    .min_size(size)
                                    .wrap(),
                                )
                            },
                        )
                        .inner;
                    if response.clicked() || response.double_clicked() {
                        chosen = Some((event.id, response.double_clicked()));
                    }
                    response.on_hover_text(format!(
                        "Exact trace coordinates: {} .. {} ns\nSource: {}",
                        event.start_ns, event.end_ns, source
                    ));
                }
            });
        if let Some((id, locate)) = chosen {
            if let Some(event) = trace.events.iter().find(|event| event.id == id) {
                self.selected = Some(id);
                self.selected_counter = None;
                self.cursor_ns = Some(event.start_ns);
                if locate {
                    self.focus(event, trace);
                    self.mode = TimingMode::Timeline;
                }
            }
        }
    }

    fn source_table(&mut self, ui: &mut egui::Ui, trace: &TimingTrace) {
        let selected = self
            .selection
            .unwrap_or_else(|| self.viewport.unwrap_or_else(|| bounds(trace)));
        self.refresh_analysis(trace, range(selected));
        let analysis = self.analysis.as_ref().unwrap();
        ui.label(RichText::new("Grouped by track, operation and recorded code source; sorted by inclusive duration, then requested bytes. Nested durations overlap. These totals are not CPU utilization.").small().color(MUTED));
        if analysis.sources.is_empty() {
            ui.label("No sources match this time window and filter.");
        }
        let mut chosen = None;
        let mut filter = None;
        egui::ScrollArea::vertical().id_salt("timing-source-list").auto_shrink([false, false]).show_rows(ui, 100.0, analysis.sources.len(), |ui, rows| {
            for row in rows {
                let source = &analysis.sources[row];
                egui::Frame::new().fill(SURFACE).corner_radius(6).inner_margin(8).show(ui, |ui| {
                    ui.set_min_width(ui.available_width());
                    ui.horizontal(|ui| {
                        if ui.selectable_label(false, RichText::new(abbreviated(&source.name, 48)).strong()).on_hover_text(&source.name).clicked() { chosen = Some(source.representative_event); }
                        ui.label(RichText::new(format!("{} events · {} inclusive · {} max overlap", source.interval_count + source.instant_count, elapsed_u128(source.inclusive_ns), elapsed(source.longest_overlap_ns))).small().color(ACCENT));
                    });
                    ui.label(RichText::new(format!("{} · {}", trace.tracks[source.track].name, source.category)).small().color(MUTED));
                    ui.horizontal(|ui| {
                        let label = source.source.as_deref().unwrap_or_else(|| unrecorded_source(&source.category));
                        if let Some(recorded) = &source.source {
                            if ui.add(egui::Label::new(RichText::new(label).color(ACCENT)).truncate().sense(Sense::click())).on_hover_text(format!("Filter this recorded code source: {label}")).clicked() { filter = Some(recorded.clone()); }
                        } else {
                            ui.add(egui::Label::new(RichText::new(label).color(MUTED)).truncate());
                        }
                    });
                    if source.request_count > 0 {
                        ui.label(RichText::new(format!("{} requested B · {} requests · {} recorded failures · {} outcomes unknown", source.requested_bytes, source.request_count, source.failed_requests, source.unknown_outcomes)).small().color(if source.failed_requests > 0 { WARNING } else { MUTED }));
                    } else { ui.add_space(13.0); }
                });
            }
        });
        if let Some(filter) = filter {
            self.filter = filter;
        }
        if let Some(id) = chosen {
            if let Some(event) = trace.events.iter().find(|event| event.id == id) {
                self.selected = Some(id);
                self.selected_counter = None;
                self.cursor_ns = Some(event.start_ns);
            }
        }
    }

    pub(super) fn show(&mut self, ctx: &egui::Context, trace: &TimingTrace, namespace: egui::Id) {
        if self.source_coverage.is_none() {
            self.source_coverage = Some(SourceCoverage::build(trace));
        }
        self.inspector(ctx, trace, namespace);
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(BACKGROUND).inner_margin(20))
            .show(ctx, |ui| {
                ui.push_id(namespace.with("timing-center"), |ui| {
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
                let coverage = self.source_coverage.as_ref().unwrap();
                ui.horizontal_wrapped(|ui| {
                    if ui.small_button(format!("IRQ events sourced {}/{}", coverage.irq_sources, coverage.irqs)).on_hover_text("Observed IRQ events with recorded or resolved handler code locations. A handler may serve multiple devices.").clicked() { self.filter = "cat:irq".into(); self.mode = TimingMode::Sources; }
                    if ui.small_button(format!("Heap events sourced {}/{}", coverage.heap_sources, coverage.heap)).on_hover_text("Observed heap events with caller code locations. Shared allocator wrappers do not establish application ownership.").clicked() { self.filter = "cat:heap".into(); self.mode = TimingMode::Sources; }
                    if ui.small_button(format!("{} application observations", coverage.app)).clicked() { self.filter = "cat:app".into(); self.mode = TimingMode::Events; }
                    if ui.small_button(format!("{} timer callbacks", coverage.timers)).clicked() { self.filter = "cat:timer".into(); self.mode = TimingMode::Sources; }
                });
                if !trace.warnings.is_empty() {
                    egui::CollapsingHeader::new(format!("Capture diagnostics ({})", trace.warnings.len())).show(ui, |ui| {
                        for warning in &trace.warnings { ui.label(RichText::new(warning).small().color(WARNING)); }
                    });
                }
                ui.add_space(10.0);
                let measured = self.selection.unwrap_or(viewport);
                self.refresh_analysis(trace, range(measured));
                let stats = &self.analysis.as_ref().unwrap().stats;
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
                    ui.add(egui::TextEdit::singleline(&mut self.filter).hint_text("Find task, IRQ handler, caller, file, address…").desired_width(280.0));
                    ui.checkbox(&mut self.show_counters, "Counters");
                    ui.checkbox(&mut self.measure_mode, "Measure range").on_hover_text("Drag across time to measure. Clear this to pan, or hold Shift for a temporary measurement.");
                    if !self.filter.is_empty() && ui.small_button("Clear filter").clicked() { self.filter.clear(); }
                    if self.selection.is_some() && ui.small_button("Clear measurement").clicked() { self.selection = None; }
                });
                ui.horizontal_wrapped(|ui| {
                    ui.selectable_value(&mut self.mode, TimingMode::Timeline, "Timeline");
                    ui.selectable_value(&mut self.mode, TimingMode::Events, "Events");
                    ui.selectable_value(&mut self.mode, TimingMode::Sources, "Source summary");
                    ui.separator();
                    ui.label(RichText::new("Minimum interval").small().color(MUTED));
                    if ui.add(egui::TextEdit::singleline(&mut self.minimum_text).desired_width(75.0)).changed() {
                        if let Ok(value) = self.minimum_text.trim().parse::<u64>() { self.minimum_ns = value; }
                    }
                    ui.label(RichText::new("ns · instants retained").small().color(MUTED));
                    if self.minimum_text.trim().parse::<u64>().is_err() {
                        ui.label(RichText::new("Enter whole nanoseconds").small().color(WARNING));
                    }
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
                if self.mode == TimingMode::Events {
                    self.event_table(ui, trace);
                    return;
                }
                if self.mode == TimingMode::Sources {
                    self.source_table(ui, trace);
                    return;
                }
                egui::ScrollArea::vertical().id_salt("timing-track-scroll").auto_shrink([false, false]).show(ui, |ui| {
                    self.canvas(ui, trace);
                });
                });
            });
    }

    fn canvas(&mut self, ui: &mut egui::Ui, trace: &TimingTrace) {
        let full = bounds(trace);
        let mut viewport = self.viewport.unwrap_or(full);
        if self.layout.as_ref().is_none_or(|layout| {
            layout.filter != self.filter || layout.minimum_ns != self.minimum_ns
        }) {
            self.layout = Some(TimingLayout::build(trace, &self.filter, self.minimum_ns));
        }
        let layout = self.layout.as_ref().unwrap();
        let heights: Vec<_> = layout
            .tracks
            .iter()
            .map(|track| 32.0 + (track.depth + 1) as f32 * 27.0)
            .collect();
        let counter_count = if self.show_counters {
            layout.counters.len()
        } else {
            0
        };
        let total_height = 42.0 + heights.iter().sum::<f32>() + counter_count as f32 * 88.0 + 12.0;
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
        for (indexed, height) in layout.tracks.iter().zip(&heights) {
            // Keep row geometry stable, but do not visit events on offscreen
            // tracks. A prefix maximum retains long spans crossing the window.
            if top + height < ui.clip_rect().top() || top > ui.clip_rect().bottom() {
                top += height;
                continue;
            }
            let track = &trace.tracks[indexed.id];
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
            for event in indexed.visible(trace, range(viewport)) {
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
        let mut sample_hit: Option<(usize, &CounterSample)> = None;
        for ((track_id, name, series), samples) in
            layout.counters.iter().filter(|_| self.show_counters)
        {
            if top + 88.0 < ui.clip_rect().top() || top > ui.clip_rect().bottom() {
                top += 88.0;
                continue;
            }
            let begin =
                samples.partition_point(|index| trace.counters[*index].time_ns < viewport.start);
            let end =
                samples.partition_point(|index| trace.counters[*index].time_ns <= viewport.end);
            let visible: Vec<_> = samples[begin..end]
                .iter()
                .map(|index| (*index, &trace.counters[*index]))
                .collect();
            let low = visible
                .iter()
                .map(|(_, sample)| sample.value)
                .reduce(f64::min)
                .unwrap_or(0.0);
            let high = visible
                .iter()
                .map(|(_, sample)| sample.value)
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
                abbreviated(&trace.tracks[*track_id].name, 25),
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
            for (index, sample) in visible {
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
                    sample_hit = Some((index, sample));
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
                if let Some(source) = event.source_name() {
                    ui.label(format!(
                        "{}: {}",
                        if event.is_callback() {
                            "Handler"
                        } else {
                            "Caller"
                        },
                        source
                    ));
                }
                if let Some(bytes) = event.requested_bytes() {
                    ui.label(format!("Requested: {bytes} B; {}", request_outcome(event)));
                }
            });
            if response.clicked() {
                self.selected = Some(event.id);
                self.selected_counter = None;
                self.cursor_ns = Some(event.start_ns);
            }
            if response.double_clicked() {
                self.focus(event, trace);
            }
        } else if let Some((index, sample)) = sample_hit {
            if response.clicked() {
                self.cursor_ns = Some(sample.time_ns);
                self.selected = None;
                self.selected_counter = Some(index);
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
                self.selected_counter = None;
            }
        }
    }

    fn inspector(&mut self, ctx: &egui::Context, trace: &TimingTrace, namespace: egui::Id) {
        egui::SidePanel::right(namespace.with("timing-inspector")).default_width(285.0).width_range(240.0..=440.0)
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
                        ui.add_space(8.0);
                        eyebrow(ui, "SOURCE AND CONTEXT");
                        let source_label = if event.is_callback() { "IRQ / TIMER HANDLER" } else { "RECORDED CALLER" };
                        field(ui, source_label, event.source_name().unwrap_or("Not recorded or unresolved"));
                        for (label, names) in [
                            ("INTERRUPT", &["irq"][..]),
                            ("TASK", &["task", "task_id"][..]),
                            ("CPU", &["cpu"][..]),
                            ("OPERATION", &["operation"][..]),
                            ("HEAP", &["heap"][..]),
                            ("ALLOCATION ADDRESS", &["address"][..]),
                            ("RECORDED PC / SYMBOL", if event.is_callback() { &["handler_pc", "handler"][..] } else { &["caller_pc", "caller"][..] }),
                            ("FILE", if event.is_callback() { &["handler_file"][..] } else { &["caller_file"][..] }),
                            ("LINE", if event.is_callback() { &["handler_line"][..] } else { &["caller_line"][..] }),
                            ("SOURCE EVIDENCE", if event.is_callback() { &["handler_evidence"][..] } else { &["caller_evidence"][..] }),
                        ] {
                            if let Some(value) = recorded_text(event, names) { field(ui, label, &value); }
                        }
                        if let Some(bytes) = event.requested_bytes() {
                            field(ui, "REQUESTED SIZE", &format!("{bytes} B"));
                            field(ui, "REQUEST RESULT", request_outcome(event));
                            ui.label(RichText::new("A request entry alone does not report success or failure. Missing allocation notes cannot establish the outcome.").small().color(MUTED));
                        } else if event.is_heap() {
                            if let Some(bytes) = recorded_text(event, &["bytes"]) {
                                field(ui, "NATIVE RECORDED SIZE", &format!("{bytes} B"));
                                ui.label(RichText::new("Allocator size may include overhead; it is not necessarily the application's requested payload.").small().color(MUTED));
                            }
                        }
                        if event.is_callback() || event.is_heap() {
                            ui.label(RichText::new(if event.source_name().is_some() {
                                "A resolved function identifies code. Shared GPIO, UART and allocator routines do not establish an IMU, GNSS or application owner without an explicit device/span record."
                            } else {
                                "Resolve the recorded PC against this capture's exact ELF. An IRQ number or task name alone cannot establish the device or allocation owner."
                            }).small().color(MUTED));
                        }
                        if let Some(source) = event.source_name() {
                            if ui.button("Find this source's events").clicked() { self.filter = source.to_owned(); self.mode = TimingMode::Events; }
                        } else if event.category.split(',').any(|part| part.trim() == "app") && ui.button("Find this named span").clicked() {
                            self.filter.clone_from(&event.name);
                            self.mode = TimingMode::Events;
                        }
                        if ui.button("Find this track's events").clicked() { self.filter.clone_from(&trace.tracks[event.track].name); self.mode = TimingMode::Events; }
                        if ui.button("Show on timeline").clicked() { self.focus(event, trace); self.mode = TimingMode::Timeline; }
                        if event.end_ns > event.start_ns && ui.button("Measure this interval").clicked() {
                            self.selection = Some(AddressWindow { start: event.start_ns, end: event.end_ns });
                        }
                        if event.args.as_object().is_some_and(|args| !args.is_empty()) {
                            ui.add_space(10.0);
                            egui::CollapsingHeader::new("All recorded arguments").show(ui, |ui| {
                                ui.label(RichText::new(serde_json::to_string_pretty(&event.args).unwrap_or_default()).monospace().size(10.0).color(MUTED));
                            });
                        }
                    } else if let Some(sample) = self.selected_counter.and_then(|index| trace.counters.get(index)) {
                        badge(ui, "RECORDED SAMPLE", ACCENT);
                        ui.heading(&sample.name);
                        field(ui, "SERIES", &sample.series);
                        field(ui, "EXACT VALUE", &sample.exact_value);
                        field(ui, "TRACE TIME", &format!("{} ns", sample.time_ns));
                        field(ui, "TRACK", &trace.tracks[sample.track].name);
                        ui.label(RichText::new("This is one measured sample. Values between samples and before the first sample remain unknown.").small().color(MUTED));
                    } else {
                        ui.heading("Inspect an interval");
                        ui.label(RichText::new("Click a task, interrupt or named span. Shift+drag across time to measure an arbitrary range.").color(MUTED));
                    }
                    ui.add_space(18.0);
                    ui.separator();
                    let selected = self.selection.unwrap_or_else(|| self.viewport.unwrap_or_else(|| bounds(trace)));
                    self.refresh_analysis(trace, range(selected));
                    let stats = &self.analysis.as_ref().unwrap().stats;
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
                    if !trace.metadata.event_counts.is_empty() {
                        ui.add_space(14.0);
                        eyebrow(ui, "NATIVE RECORDER EVENTS");
                        ui.label(RichText::new("Native notes are distinct from reconstructed intervals and counter samples.").small().color(MUTED));
                        if let Some(count) = trace.metadata.observed_note_count { field(ui, "TIMESTAMPED NOTES", &count.to_string()); }
                        if let Some(count) = trace.metadata.ignored_line_count { field(ui, "IGNORED CONSOLE LINES", &count.to_string()); }
                        for (kind, count) in &trace.metadata.event_counts {
                            ui.horizontal(|ui| { ui.label(kind.replace('_', " ")); ui.label(RichText::new(count.to_string()).color(ACCENT)); });
                        }
                    }
                    if !trace.metadata.source_attribution.is_empty() {
                        ui.add_space(14.0);
                        eyebrow(ui, "SOURCE COVERAGE");
                        for (kind, count) in &trace.metadata.source_attribution {
                            ui.horizontal(|ui| { ui.label(kind.replace('_', " ")); ui.label(RichText::new(count.to_string()).color(ACCENT)); });
                        }
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
            .find(|event| {
                event.kind == TimingEventKind::Interval
                    && event.end_ns > event.start_ns
                    && event.source_name().is_some()
            })
            .or_else(|| {
                trace.events.iter().find(|event| {
                    event.kind == TimingEventKind::Interval && event.end_ns > event.start_ns
                })
            })
            .ok_or("Timing smoke needs an observed interval")?;
        self.selected = Some(event.id);
        self.cursor_ns = Some(event.start_ns);
        if self.mode == TimingMode::Sources && event.is_callback() {
            self.filter = if event.is_irq() {
                "cat:irq"
            } else {
                "cat:timer"
            }
            .into();
        }
        self.selection = (self.mode == TimingMode::Timeline).then_some(AddressWindow {
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

    #[test]
    fn indexed_window_retains_spanning_parent_and_boundary_instants() {
        let trace = nxrs_memory_explorer::parse_timing(
            r#"[
            {"ph":"X","pid":1,"ts":0,"dur":100,"name":"long parent"},
            {"ph":"X","pid":1,"ts":20,"dur":20,"name":"past child"},
            {"ph":"X","pid":1,"ts":50,"dur":10,"name":"inside child"},
            {"ph":"i","pid":1,"ts":60,"name":"right boundary"},
            {"ph":"X","pid":2,"ts":70,"dur":10,"name":"different track"}
        ]"#,
        )
        .unwrap();
        let layout = TimingLayout::build(&trace, "", 0);
        let window = TimingRange {
            start_ns: 50_000,
            end_ns: 60_000,
        };
        let names: Vec<_> = layout.tracks[0]
            .visible(&trace, window)
            .map(|event| event.name.as_str())
            .collect();
        assert_eq!(names, ["long parent", "inside child", "right boundary"]);
        let minimum = TimingLayout::build(&trace, "", 50_000);
        let names: Vec<_> = minimum.tracks[0]
            .visible(&trace, window)
            .map(|event| event.name.as_str())
            .collect();
        assert_eq!(names, ["long parent", "right boundary"]);
        let names: Vec<_> = layout.tracks[0]
            .visible(
                &trace,
                TimingRange {
                    start_ns: 100_000,
                    end_ns: 200_000,
                },
            )
            .collect();
        assert!(names.is_empty());
    }

    #[test]
    fn analysis_filters_and_selected_counter_are_reset_for_new_capture() {
        let trace = nxrs_memory_explorer::parse_timing(r#"[
          {"ph":"X","pid":1,"ts":0,"dur":5,"name":"IRQ","cat":"irq","args":{"handler_function":"timer_callback"}},
          {"ph":"i","pid":2,"ts":3,"name":"request","cat":"heap","args":{"requested_bytes":64,"caller_function":"capture_open"}},
          {"ph":"C","pid":3,"ts":4,"name":"heap","args":{"used":100}}
        ]"#).unwrap();
        let mut view = TimingView::default();
        view.reset(&trace);
        view.filter = "capture_open".into();
        view.refresh_analysis(&trace, trace.bounds);
        assert_eq!(view.analysis.as_ref().unwrap().events.len(), 1);
        assert_eq!(
            view.analysis.as_ref().unwrap().sources[0].requested_bytes,
            64
        );
        view.minimum_ns = 50_000;
        view.refresh_analysis(&trace, trace.bounds);
        assert_eq!(
            view.analysis.as_ref().unwrap().events.len(),
            1,
            "instants survive duration filtering"
        );
        view.selected_counter = Some(0);
        view.reset(&trace);
        assert!(
            view.analysis.is_none() && view.layout.is_none() && view.selected_counter.is_none()
        );
        let coverage = SourceCoverage::build(&trace);
        assert_eq!(
            (
                coverage.irqs,
                coverage.irq_sources,
                coverage.heap,
                coverage.heap_sources
            ),
            (1, 1, 1, 1)
        );
    }

    #[test]
    fn event_and_source_views_render_with_source_and_counter_inspectors() {
        let trace = nxrs_memory_explorer::parse_timing(r#"[
          {"ph":"X","pid":1,"ts":0,"dur":5,"name":"IRQ","cat":"irq","args":{"irq":15,"handler_function":"systick_interrupt","handler_file":"chip/timer.c","handler_line":42}},
          {"ph":"i","pid":2,"ts":3,"name":"request","cat":"heap","args":{"requested_bytes":64,"caller_function":"capture_open"}},
          {"ph":"C","pid":3,"ts":4,"name":"heap","args":{"used":100}}
        ]"#).unwrap();
        let context = egui::Context::default();
        let mut view = TimingView::default();
        view.reset(&trace);
        view.selected = Some(trace.events[0].id);
        for mode in ["events", "sources", "timeline"] {
            view.set_analysis_mode(mode);
            let output = context.run(
                egui::RawInput {
                    screen_rect: Some(Rect::from_min_size(Pos2::ZERO, Vec2::new(1440.0, 940.0))),
                    ..Default::default()
                },
                |context| view.show(context, &trace, egui::Id::new("timing-test")),
            );
            assert!(!output.shapes.is_empty());
        }
        view.selected = None;
        view.selected_counter = Some(0);
        let output = context.run(egui::RawInput::default(), |context| {
            view.show(context, &trace, egui::Id::new("timing-test"))
        });
        assert!(!output.shapes.is_empty());
    }
}
