use super::*;
use nxrs_memory_explorer::{
    CounterSample, TimingEvent, TimingEventKind, TimingOccurrences, TimingRange,
    TimingRateDenominator, TimingSourceStats, TimingTrace,
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
    failed_calls: usize,
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
                result.failed_calls +=
                    usize::from(event.is_heap_call() && event.request_failed() == Some(true));
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

fn elapsed_mean(ns: f64) -> String {
    if ns < 1_000.0 {
        format!("{ns:.3} ns")
    } else if ns < 1_000_000.0 {
        format!("{:.3} us", ns / 1_000.0)
    } else if ns < 1_000_000_000.0 {
        format!("{:.3} ms", ns / 1_000_000.0)
    } else {
        format!("{:.6} s", ns / 1_000_000_000.0)
    }
}

fn occurrence_rates(counts: &TimingOccurrences, denominator: TimingRateDenominator) -> String {
    match denominator {
        TimingRateDenominator::Observed { duration_ns } => format!(
            "{} complete interval starts · {:.3}/s   |   {} instants · {:.3}/s   |   denominator {} ({} ns)",
            counts.interval_starts,
            denominator.per_second(counts.interval_starts).unwrap(),
            counts.instants,
            denominator.per_second(counts.instants).unwrap(),
            elapsed(duration_ns),
            duration_ns,
        ),
        TimingRateDenominator::Unavailable(reason) => format!(
            "{} complete interval starts · {} instants · rates unavailable: {}",
            counts.interval_starts, counts.instants, reason.description()
        ),
    }
}

fn full_duration_summary(counts: &TimingOccurrences) -> String {
    match (counts.mean_duration_ns(), counts.longest_duration_ns) {
        (Some(mean), Some(maximum)) => format!(
            "Full durations for {} starts with both endpoints: mean {:.3} ns · max {} ({} ns)",
            counts.complete_intervals, mean, elapsed(maximum), maximum,
        ),
        _ => "Full duration mean/max unavailable: no observed interval starts with both endpoints in this cohort".into(),
    }
}

fn instant_spacing_summary(
    source: &TimingSourceStats,
    denominator: TimingRateDenominator,
) -> (String, String) {
    let unavailable = match denominator {
        TimingRateDenominator::Unavailable(reason) => reason.description().to_owned(),
        TimingRateDenominator::Observed { .. } => {
            if let Some(spacing) = &source.instant_spacing {
                if let Some(mean) = spacing.mean_ns() {
                    let maximum = spacing.max_gap.end_ns - spacing.max_gap.start_ns;
                    let label = format!(
                        "Observed spacing: {} gaps · min {} · mean {} · max {}",
                        spacing.gap_count,
                        elapsed(spacing.min_ns),
                        elapsed_mean(mean),
                        elapsed(maximum),
                    );
                    let detail = format!(
                        "Consecutive semantic instants in this source group and selected half-open window.\n{} observed gaps from {} instants.\nMinimum: {} ns\nMean: {:.3} ns; exact ratio {} ns / {} gaps\nMaximum: {} ns\nLongest gap endpoints: {} .. {} ns (trace coordinates)\nCapture boundaries, intervals, counters and censored markers do not supply gap endpoints.",
                        spacing.gap_count,
                        source.occurrences.instants,
                        spacing.min_ns,
                        mean,
                        spacing.sum_ns,
                        spacing.gap_count,
                        maximum,
                        spacing.max_gap.start_ns,
                        spacing.max_gap.end_ns,
                    );
                    return (label, detail);
                }
            }
            if source.occurrences.instants == 0 && source.occurrences.excluded_markers > 0 {
                "Only unknown/censored markers in this window".into()
            } else if source.occurrences.instants < 2 {
                format!(
                    "Need at least 2 semantic instants in the selected window; {} observed",
                    source.occurrences.instants,
                )
            } else {
                "No admitted gap summary for these observations".into()
            }
        }
    };
    let label = format!("Observed spacing unavailable: {unavailable}");
    let detail = format!(
        "{label}\nSpacing uses consecutive admitted semantic instants from this source group within the selected half-open window. A missing summary does not establish zero spacing."
    );
    (label, detail)
}

fn source_occurrences(counts: &TimingOccurrences, denominator: TimingRateDenominator) -> String {
    match (
        denominator.per_second(counts.interval_starts),
        denominator.per_second(counts.instants),
    ) {
        (Some(starts), Some(instants)) => format!(
            "{} complete starts · {starts:.3}/s · {} instants · {instants:.3}/s · {} unknown markers excluded",
            counts.interval_starts, counts.instants, counts.excluded_markers,
        ),
        _ => format!(
            "{} complete starts · {} instants · {} unknown markers excluded · rates unavailable",
            counts.interval_starts, counts.instants, counts.excluded_markers,
        ),
    }
}

fn request_outcome(event: &TimingEvent) -> &'static str {
    match event.request_failed() {
        Some(true) => "failed",
        Some(false) => "succeeded",
        None if event
            .argument("request_observed")
            .and_then(serde_json::Value::as_bool)
            == Some(false) =>
        {
            "result without paired request"
        }
        None if event.result_argument("incomplete_reason").is_some() => {
            "outcome unknown · incomplete call"
        }
        None => "outcome not captured",
    }
}

fn recorded_text(event: &TimingEvent, names: &[&str]) -> Option<String> {
    names
        .iter()
        .find_map(|name| event.argument(name))
        .map(argument_text)
}

fn argument_text(value: &serde_json::Value) -> String {
    value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string())
}

fn recorded_result_text(event: &TimingEvent, name: &str) -> Option<String> {
    event.result_argument(name).map(argument_text)
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
    if event.request_failed() == Some(true) {
        WARNING
    } else if event.category.contains("irq") || event.category.contains("interrupt") {
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
                        format!(" · allocator call {} B · {}", bytes, request_outcome(event))
                    } else if event.is_heap_call() {
                        format!(" · {}", request_outcome(event))
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
        ui.label(RichText::new("Grouped by track, operation and recorded code source; sorted by clipped inclusive duration, then requested bytes. Occurrence counts use starts and instants in the selected half-open window. Named batch scopes are scopes, not device samples or fixes.").small().color(MUTED));
        ui.label(RichText::new("Allocator summaries include intersecting calls and internal backing calls. Requested call bytes are inputs, not net heap growth or application allocation counts.").small().color(MUTED));
        if analysis.sources.is_empty() {
            ui.label("No sources match this time window and filter.");
        }
        let mut chosen = None;
        let mut filter = None;
        egui::ScrollArea::vertical().id_salt("timing-source-list").auto_shrink([false, false]).show_rows(ui, 145.0, analysis.sources.len(), |ui, rows| {
            for row in rows {
                let source = &analysis.sources[row];
                egui::Frame::new().fill(SURFACE).corner_radius(6).inner_margin(8).show(ui, |ui| {
                    ui.set_min_width(ui.available_width());
                    ui.horizontal(|ui| {
                        if ui.selectable_label(false, RichText::new(abbreviated(&source.name, 48)).strong()).on_hover_text(&source.name).clicked() { chosen = Some(source.representative_event); }
                    });
                    let occurrences = source_occurrences(&source.occurrences, analysis.stats.rate_denominator);
                    ui.add(egui::Label::new(RichText::new(&occurrences).small().color(ACCENT)).truncate()).on_hover_text(occurrences);
                    let instant_only = source.interval_count == 0;
                    let spacing = (instant_only || source.occurrences.instants > 0)
                        .then(|| instant_spacing_summary(source, analysis.stats.rate_denominator));
                    if instant_only {
                        let (label, detail) = spacing.as_ref().unwrap();
                        ui.add(egui::Label::new(RichText::new(label).small()).truncate()).on_hover_text(detail);
                    } else {
                        let duration = full_duration_summary(&source.occurrences);
                        let detail = spacing.as_ref().map_or_else(|| duration.clone(), |(label, detail)| format!("{duration}\n\n{label}\n{detail}"));
                        ui.add(egui::Label::new(RichText::new(&duration).small()).truncate()).on_hover_text(detail);
                    }
                    ui.label(RichText::new(format!("Clipped overlap: {} inclusive · {} max · {} intersecting intervals", elapsed_u128(source.inclusive_ns), elapsed(source.longest_overlap_ns), source.interval_count)).small().color(MUTED));
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
                        ui.label(RichText::new(format!("{} requested call B · {} intersecting calls · {} failures · {} outcomes unknown", source.requested_bytes, source.request_count, source.failed_requests, source.unknown_outcomes)).small().color(if source.failed_requests > 0 { WARNING } else { MUTED }));
                    } else if source.unpaired_results > 0 {
                        ui.label(RichText::new(format!("{} result endpoints without paired requests", source.unpaired_results)).small().color(WARNING));
                    } else if let (false, Some((label, detail))) = (instant_only, spacing.as_ref()) {
                        ui.add(egui::Label::new(RichText::new(label).small()).truncate()).on_hover_text(detail);
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
                if let Some(window) = &trace.metadata.capture_window {
                    ui.label(RichText::new(format!("Capture: requested {} · actual {} · stopped by {} · enabled categories: {}", elapsed(window.requested_duration_ns), elapsed(window.duration_ns()), window.stop_reason.as_str(), if window.categories.is_empty() { "none".into() } else { window.categories.join(", ") })).small().color(MUTED));
                    ui.label(RichText::new("Axis covers the recorded capture window, including silence. Blank time is not inferred idle.").small().color(MUTED));
                } else {
                    ui.label(RichText::new(format!("Axis shows elapsed time from the first record at {} ns; acquisition duration is unknown.", trace.bounds.start_ns)).small().color(MUTED));
                }
                ui.horizontal_wrapped(|ui| {
                    if trace.metadata.loss_detected == Some(true) || trace.metadata.lost_records.is_some_and(|count| count > 0) {
                        badge(ui, "RECORD LOSS REPORTED", WARNING);
                    } else if trace.metadata.loss_detected == Some(false) || trace.metadata.lost_records == Some(0) {
                        badge(ui, "NO LOSS REPORTED", ACCENT);
                    } else {
                        badge(ui, "LOSS STATUS UNKNOWN", WARNING);
                    }
                    if let Some(window) = &trace.metadata.capture_window {
                        badge(ui, if window.complete { "CAPTURE WINDOW RETAINED" } else { "INCOMPLETE WINDOW COVERAGE" }, if window.complete { ACCENT } else { WARNING });
                        if trace.metadata.incomplete_intervals > 0 {
                            badge(ui, "CENSORED INTERVAL EDGES", WARNING);
                        }
                    } else if trace.metadata.complete != Some(true) || trace.metadata.incomplete_intervals > 0 {
                        badge(ui, "PARTIAL OR UNVERIFIED COVERAGE", WARNING);
                    }
                });
                let coverage = self.source_coverage.as_ref().unwrap();
                ui.horizontal_wrapped(|ui| {
                    if ui.small_button(format!("IRQ events sourced {}/{}", coverage.irq_sources, coverage.irqs)).on_hover_text("Observed IRQ events with recorded or resolved handler code locations. A handler may serve multiple devices.").clicked() { self.filter = "cat:irq".into(); self.mode = TimingMode::Sources; }
                    if ui.small_button(format!("Heap events sourced {}/{}", coverage.heap_sources, coverage.heap)).on_hover_text("Observed heap events with caller code locations. Shared allocator wrappers do not establish application ownership.").clicked() { self.filter = "cat:heap".into(); self.mode = TimingMode::Sources; }
                    if coverage.heap > 0 && ui.small_button(format!("Failed calls ({})", coverage.failed_calls)).on_hover_text("Show allocator calls with a recorded failure outcome. Incomplete calls and unpaired result endpoints stay unknown.").clicked() { self.filter = "outcome:failed".into(); self.mode = TimingMode::Events; }
                    if ui.small_button(format!("{} application observations", coverage.app)).clicked() { self.filter = "cat:app".into(); self.mode = TimingMode::Events; }
                    if ui.small_button(format!("{} timer observations", coverage.timers)).on_hover_text("Retained timer records include censored edge markers. Complete starts and semantic instants are counted separately in the summary.").clicked() { self.filter = "cat:timer".into(); self.mode = TimingMode::Sources; }
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
                        (if self.selection.is_some() { "MEASURED SELECTION" } else { "INTERSECTING INTERVALS" }, if self.selection.is_some() { elapsed(measured.span()) } else { stats.interval_count.to_string() }, if self.selection.is_some() { format!("{} ns; endpoint subtraction", measured.span()) } else { "Overlap count; separate from starts".into() }),
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
                ui.add_space(8.0);
                ui.label(RichText::new(occurrence_rates(&stats.occurrences, stats.rate_denominator)).small().color(if matches!(stats.rate_denominator, TimingRateDenominator::Observed { .. }) { ACCENT } else { MUTED }));
                ui.label(RichText::new(format!("Counts use [{}, {}) ns · {} unknown/censored markers excluded · Source summary shows full durations separately from overlap.", measured.start, measured.end, stats.occurrences.excluded_markers)).small().color(MUTED));
                ui.add_space(12.0);
                ui.horizontal_wrapped(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut self.filter).hint_text("Find source, address, outcome:failed…").desired_width(280.0));
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
                        badge(ui, if !event.is_observed_occurrence() { "UNKNOWN / CENSORED MARKER" } else if event.kind == TimingEventKind::Instant { "INSTANT" } else { "OBSERVED INTERVAL" }, event_color(event));
                        ui.add_space(10.0);
                        ui.heading(&event.name);
                        ui.add_space(14.0);
                        if event.is_heap_call() && event.kind == TimingEventKind::Instant {
                            field(ui, "CALL DURATION", "Unknown; only an endpoint was captured");
                        } else {
                            field(ui, "DURATION", &if !event.is_observed_occurrence() {
                                "Unknown; marker excluded from occurrence and duration summaries".into()
                            } else if event.kind == TimingEventKind::Instant {
                                "Instant observation; no interval duration".into()
                            } else {
                                format!("{}  ({} ns)", elapsed(event.duration_ns()), event.duration_ns())
                            });
                        }
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
                            ("ALLOCATOR CALL ID", &["request_id"][..]),
                            ("INPUT ADDRESS", &["old_address"][..]),
                            ("ALIGNMENT · BYTES", &["alignment"][..]),
                            ("NATIVE NOTE ADDRESS", &["address"][..]),
                            ("RECORDED PC / SYMBOL", if event.is_callback() { &["handler_pc", "handler"][..] } else { &["caller_pc", "caller"][..] }),
                            ("FILE", if event.is_callback() { &["handler_file"][..] } else { &["caller_file"][..] }),
                            ("LINE", if event.is_callback() { &["handler_line"][..] } else { &["caller_line"][..] }),
                            ("SOURCE EVIDENCE", if event.is_callback() { &["handler_evidence"][..] } else { &["caller_evidence"][..] }),
                        ] {
                            if let Some(value) = recorded_text(event, names) { field(ui, label, &value); }
                        }
                        if event.is_heap_call() {
                            if let Some(bytes) = event.requested_bytes() { field(ui, "REQUESTED SIZE", &format!("{bytes} B")); }
                            field(ui, "CALL RESULT", request_outcome(event));
                            for (label, name) in [
                                ("RETURNED ADDRESS", "result_address"),
                                ("INCOMPLETE EVIDENCE", "incomplete_reason"),
                                ("UNPAIRED RETURN ADDRESS", "observed_result_address"),
                                ("UNPAIRED RETURN OUTCOME", "observed_outcome"),
                            ] {
                                if let Some(value) = recorded_result_text(event, name) { field(ui, label, &value); }
                            }
                            if event.request_failed() == Some(true) {
                                field(ui, "FAILURE REASON", event.failure_reason().unwrap_or("Not recorded"));
                            }
                            ui.label(RichText::new("Native allocator calls include internal backing calls. A returned address does not establish allocation lifetime, heap topology, or current ownership. Missing endpoints keep the correlated outcome unknown.").small().color(MUTED));
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
                    ui.add_space(10.0);
                    eyebrow(ui, "OBSERVED START COHORT");
                    ui.label(RichText::new(full_duration_summary(&stats.occurrences)).small());
                    ui.label(RichText::new("Full durations belong to complete intervals starting in the selected half-open window, even when they extend past its end. Earlier starts contribute only clipped coverage. These measures are not CPU utilization.").small().color(MUTED));
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
                    if let Some(window) = &trace.metadata.capture_window {
                        field(ui, "CAPTURE · SOURCE CLOCK", &format!("[{}, {}) ns", window.start_ns, window.end_ns));
                        field(ui, "REQUESTED DURATION", &format!("{} ({} ns)", elapsed(window.requested_duration_ns), window.requested_duration_ns));
                        field(ui, "ACTUAL DURATION", &format!("{} ({} ns)", elapsed(window.duration_ns()), window.duration_ns()));
                        field(ui, "STOP REASON", window.stop_reason.as_str());
                        field(ui, "NATIVE ENABLED CATEGORIES", &if window.categories.is_empty() { "None".into() } else { window.categories.join(", ") });
                        ui.label(RichText::new("Enabled categories describe acquisition configuration. They do not establish a device owner or turn named batch scopes into individual samples.").small().color(MUTED));
                    } else {
                        field(ui, "ACQUISITION WINDOW", "Unknown; first/last events do not establish capture duration");
                    }
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
            });
        let Some(event) = event else {
            if trace.capture_range() != Some(trace.bounds) {
                return Err(
                    "Timing smoke needs an observed interval or an explicit capture window".into(),
                );
            }
            self.viewport = Some(full);
            self.selection = None;
            self.selected = trace.events.first().map(|event| event.id);
            self.cursor_ns = Some(full.start);
            self.refresh_analysis(trace, range(full));
            return Ok(());
        };
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

    fn timed_fixture(events: serde_json::Value) -> serde_json::Value {
        serde_json::json!({
            "otherData": {"nxrs": {
                "schema_version":1, "recording_id":"rendered-window", "clock":"fixture-clock",
                "origin_ns":100, "loss_detected":false, "complete":true,
                "capture_window": {
                    "start_ns":100,"end_ns":3_000_000_100_u64,"requested_duration_ns":2_000_000_000_u64,
                    "stop_reason":"duration","complete":true,"categories":["app"]
                }
            }},
            "traceEvents":events
        })
    }

    fn rendered_output(view: &mut TimingView, trace: &TimingTrace, size: Vec2) -> egui::FullOutput {
        let context = egui::Context::default();
        let input = egui::RawInput {
            screen_rect: Some(Rect::from_min_size(Pos2::ZERO, size)),
            ..Default::default()
        };
        let _ = context.run(input.clone(), |context| {
            view.show(context, trace, egui::Id::new("capture-window-render"));
        });
        context.run(input, |context| {
            view.show(context, trace, egui::Id::new("capture-window-render"));
        })
    }

    fn rendered_text(view: &mut TimingView, trace: &TimingTrace, size: Vec2) -> String {
        fn text(shape: &egui::epaint::Shape, result: &mut String) {
            match shape {
                egui::epaint::Shape::Text(text) => {
                    result.push_str(&text.galley.job.text);
                    result.push('\n');
                }
                egui::epaint::Shape::Vec(shapes) => {
                    for shape in shapes {
                        text(shape, result);
                    }
                }
                _ => {}
            }
        }
        let output = rendered_output(view, trace, size);
        let mut result = String::new();
        for shape in &output.shapes {
            text(&shape.shape, &mut result);
        }
        result
    }

    fn visible_text(view: &mut TimingView, trace: &TimingTrace, size: Vec2) -> String {
        fn text(shape: &egui::epaint::Shape, clip: Rect, result: &mut String) {
            match shape {
                egui::epaint::Shape::Text(text)
                    if clip.contains_rect(text.visual_bounding_rect()) =>
                {
                    // Inspect painted glyphs rather than the original layout text:
                    // truncated and off-screen strings must not satisfy visibility.
                    for row in &text.galley.rows {
                        result.extend(row.glyphs.iter().map(|glyph| glyph.chr));
                        result.push('\n');
                    }
                }
                egui::epaint::Shape::Vec(shapes) => {
                    for shape in shapes {
                        text(shape, clip, result);
                    }
                }
                _ => {}
            }
        }
        let mut result = String::new();
        for shape in &rendered_output(view, trace, size).shapes {
            text(&shape.shape, shape.clip_rect, &mut result);
        }
        result
    }

    fn semantic_instants(timestamps_us: &[u64]) -> serde_json::Value {
        serde_json::Value::Array(
            timestamps_us
                .iter()
                .map(|timestamp| {
                    serde_json::json!({
                        "ph":"i", "pid":1, "name":"gnss/fix_received", "cat":"app", "ts":timestamp
                    })
                })
                .collect(),
        )
    }

    #[test]
    fn instant_spacing_renders_periodic_bursty_and_coincident_observations() {
        for (timestamps, expected, exact_ratio, endpoints) in [
            (
                [250_000, 1_250_000, 2_250_000],
                "Observed spacing: 2 gaps · min 1.000000 s · mean 1.000000 s · max 1.000000 s",
                "exact ratio 2000000000 ns / 2 gaps",
                "Longest gap endpoints: 250000000 .. 1250000000 ns (trace coordinates)",
            ),
            (
                [100_000, 100_000, 2_500_000],
                "Observed spacing: 2 gaps · min 0 ns · mean 1.200000 s · max 2.400000 s",
                "exact ratio 2400000000 ns / 2 gaps",
                "Longest gap endpoints: 100000000 .. 2500000000 ns (trace coordinates)",
            ),
            (
                [100_000, 100_000, 100_000],
                "Observed spacing: 2 gaps · min 0 ns · mean 0.000 ns · max 0 ns",
                "exact ratio 0 ns / 2 gaps",
                "Longest gap endpoints: 100000000 .. 100000000 ns (trace coordinates)",
            ),
        ] {
            let trace = nxrs_memory_explorer::parse_timing(
                &timed_fixture(semantic_instants(&timestamps)).to_string(),
            )
            .unwrap();
            let mut view = TimingView::default();
            view.reset(&trace);
            view.mode = TimingMode::Sources;
            for size in [Vec2::new(1000.0, 700.0), Vec2::new(1440.0, 940.0)] {
                let rendered = visible_text(&mut view, &trace, size);
                assert!(
                    rendered.contains(expected),
                    "Missing {expected:?}: {rendered}"
                );
            }
            let sources = trace.source_stats(trace.bounds, "", 0);
            let (_, detail) =
                instant_spacing_summary(&sources[0], trace.rate_denominator(trace.bounds));
            assert!(detail.contains(exact_ratio), "{detail}");
            assert!(detail.contains(endpoints), "{detail}");
        }
    }

    #[test]
    fn instant_spacing_renders_loss_coverage_legacy_and_insufficient_reasons() {
        let base = timed_fixture(semantic_instants(&[250_000, 1_250_000, 2_250_000]));
        let mut loss = base.clone();
        loss["otherData"]["nxrs"]["loss_detected"] = serde_json::json!(true);
        loss["otherData"]["nxrs"]["capture_window"]["complete"] = serde_json::json!(false);
        let mut incomplete = base.clone();
        incomplete["otherData"]["nxrs"]["capture_window"]["complete"] = serde_json::json!(false);
        let mut unknown_loss = base.clone();
        unknown_loss["otherData"]["nxrs"]
            .as_object_mut()
            .unwrap()
            .remove("loss_detected");
        let mut legacy = base;
        legacy["otherData"]["nxrs"]
            .as_object_mut()
            .unwrap()
            .remove("capture_window");
        let one = timed_fixture(semantic_instants(&[1_000_000]));
        let mut censored = timed_fixture(serde_json::json!([
            {"ph":"i", "pid":1, "name":"unfinished work", "cat":"app,unknown,capture", "ts":1_000_000,
             "args":{"duration_unknown":true,"reason":"capture ended"}}
        ]));
        censored["otherData"]["nxrs"]["complete"] = serde_json::json!(false);
        censored["otherData"]["nxrs"]["incomplete_intervals"] = serde_json::json!(1);
        for (document, reason) in [
            (
                loss,
                "Record loss was reported; the occurrence population is incomplete",
            ),
            (incomplete, "Capture-window coverage is incomplete"),
            (unknown_loss, "Record-loss status is unknown"),
            (
                legacy,
                "No explicit capture window; event extent is not an acquisition duration",
            ),
            (
                one,
                "Need at least 2 semantic instants in the selected window; 1 observed",
            ),
            (censored, "Only unknown/censored markers in this window"),
        ] {
            let trace = nxrs_memory_explorer::parse_timing(&document.to_string()).unwrap();
            let mut view = TimingView::default();
            view.reset(&trace);
            view.mode = TimingMode::Sources;
            let rendered = visible_text(&mut view, &trace, Vec2::new(1600.0, 1000.0));
            let expected = format!("Observed spacing unavailable: {reason}");
            assert!(
                rendered.contains(&expected),
                "Missing {expected:?}: {rendered}"
            );
            assert!(!rendered.contains("Observed spacing: 0 gaps"));
        }
    }

    #[test]
    fn mixed_source_retains_full_durations_and_displays_only_instant_spacing() {
        let trace = nxrs_memory_explorer::parse_timing(
            &timed_fixture(serde_json::json!([
                {"ph":"X", "pid":1, "name":"mixed operation", "cat":"app", "ts":250_000, "dur":1_500_000},
                {"ph":"i", "pid":1, "name":"mixed operation", "cat":"app", "ts":500_000},
                {"ph":"i", "pid":1, "name":"mixed operation", "cat":"app", "ts":1_500_000},
                {"ph":"i", "pid":1, "name":"mixed operation", "cat":"app", "ts":2_500_000}
            ]))
            .to_string(),
        )
        .unwrap();
        let mut view = TimingView::default();
        view.reset(&trace);
        view.mode = TimingMode::Sources;
        let rendered = visible_text(&mut view, &trace, Vec2::new(1600.0, 1000.0));
        for expected in [
            "Full durations for 1 starts with both endpoints: mean 1500000000.000 ns",
            "Observed spacing: 2 gaps · min 1.000000 s · mean 1.000000 s · max 1.000000 s",
            "Clipped overlap: 1.500000 s inclusive · 1.500000 s max · 1 intersecting intervals",
        ] {
            assert!(
                rendered.contains(expected),
                "Missing {expected:?}: {rendered}"
            );
        }
        let sources = trace.source_stats(trace.bounds, "", 0);
        assert_eq!(sources.len(), 1);
        let (_, detail) =
            instant_spacing_summary(&sources[0], trace.rate_denominator(trace.bounds));
        assert!(
            detail.contains("2 observed gaps from 3 instants"),
            "{detail}"
        );
        assert!(detail.contains("500000000 .. 1500000000 ns"), "{detail}");
    }

    #[test]
    fn silent_capture_renders_actual_window_and_known_zero_without_invented_intervals() {
        let trace =
            nxrs_memory_explorer::parse_timing(&timed_fixture(serde_json::json!([])).to_string())
                .unwrap();
        let mut view = TimingView::default();
        view.reset(&trace);
        view.smoke(&trace).unwrap();
        assert_eq!(
            view.viewport,
            Some(AddressWindow {
                start: 0,
                end: 3_000_000_000
            })
        );
        assert_eq!(view.selected, None);
        for size in [Vec2::new(1000.0, 700.0), Vec2::new(1440.0, 940.0)] {
            let rendered = rendered_text(&mut view, &trace, size);
            assert!(
                rendered.contains("requested 2.000000 s · actual 3.000000 s"),
                "{rendered}"
            );
            assert!(
                rendered.contains("0 complete interval starts · 0.000/s"),
                "{rendered}"
            );
            assert!(
                rendered.contains("denominator 3.000000 s (3000000000 ns)"),
                "{rendered}"
            );
            assert!(rendered.contains("including silence"), "{rendered}");
        }
    }

    #[test]
    fn unknown_window_coverage_renders_unavailable_rates_and_censored_marker_reason() {
        let mut document = timed_fixture(serde_json::json!([
            {"ph":"i","pid":1,"name":"Unclosed work","cat":"app,unknown,capture","ts":1_000_000,
             "args":{"duration_unknown":true,"reason":"capture ended"}}
        ]));
        document["otherData"]["nxrs"]["complete"] = serde_json::json!(false);
        document["otherData"]["nxrs"]["capture_window"]["complete"] = serde_json::json!(false);
        document["otherData"]["nxrs"]["incomplete_intervals"] = serde_json::json!(1);
        let trace = nxrs_memory_explorer::parse_timing(&document.to_string()).unwrap();
        let mut view = TimingView::default();
        view.reset(&trace);
        view.selected = Some(trace.events[0].id);
        let rendered = rendered_text(&mut view, &trace, Vec2::new(1440.0, 1000.0));
        assert!(
            rendered.contains("rates unavailable: Capture-window coverage is incomplete"),
            "{rendered}"
        );
        assert!(
            rendered.contains("1 unknown/censored markers excluded"),
            "{rendered}"
        );
        assert!(rendered.contains("UNKNOWN / CENSORED MARKER"), "{rendered}");
        assert!(
            rendered.contains("Unknown; marker excluded from occurrence and duration summaries"),
            "{rendered}"
        );
    }

    #[test]
    fn source_summary_retains_scope_identity_and_full_duration_beyond_selection() {
        let trace = nxrs_memory_explorer::parse_timing(
            &timed_fixture(serde_json::json!([
                {"ph":"X","pid":1,"name":"gnss/batch","cat":"app","ts":500_000,"dur":2_000_000}
            ]))
            .to_string(),
        )
        .unwrap();
        let mut view = TimingView::default();
        view.reset(&trace);
        view.mode = TimingMode::Sources;
        view.selection = Some(AddressWindow {
            start: 0,
            end: 1_000_000_000,
        });
        let rendered = rendered_text(&mut view, &trace, Vec2::new(1600.0, 1100.0));
        assert!(rendered.contains("gnss/batch"), "{rendered}");
        assert!(
            rendered.contains("1 complete starts · 1.000/s"),
            "{rendered}"
        );
        assert!(rendered.contains("mean 2000000000.000 ns"), "{rendered}");
        assert!(
            rendered.contains("Clipped overlap: 500.000 ms"),
            "{rendered}"
        );
        assert!(!rendered.contains("fixes/s"));
    }

    #[test]
    fn timed_allocator_summaries_preserve_outcomes_without_counting_incomplete_endpoints() {
        let mut document = timed_fixture(serde_json::json!([
            {"ph":"X","pid":1,"ts":250_000,"dur":1_500_000,"name":"malloc","cat":"heap,request",
             "args":{"request_observed":true,"requested_bytes":64,"outcome":"failed","failure_reason":"allocator returned NULL"}},
            {"ph":"X","pid":1,"ts":1_000_000,"dur":0,"name":"malloc","cat":"heap,request",
             "args":{"request_observed":true,"requested_bytes":128,"outcome":"succeeded"}},
            {"ph":"i","pid":1,"ts":500_000,"name":"unfinished malloc","cat":"heap,request",
             "args":{"request_observed":true,"requested_bytes":32,"incomplete_reason":"missing result"}},
            {"ph":"i","pid":1,"ts":750_000,"name":"unpaired result","cat":"heap,result",
             "args":{"request_observed":false,"requested_bytes":999,"observed_outcome":"failed"}},
            {"ph":"i","pid":1,"ts":800_000,"name":"legacy request","cat":"heap,request",
             "args":{"requested_bytes":8}},
            {"ph":"X","pid":1,"ts":900_000,"dur":100_000,"name":"incomplete interval","cat":"heap,request",
             "args":{"request_observed":true,"requested_bytes":16,"end_args":{"incomplete_reason":"untrusted endpoint"}}},
            {"ph":"i","pid":1,"ts":1_000_000,"name":"gnss/fix_received","cat":"app"}
        ]));
        document["otherData"]["nxrs"]["capture_window"]["categories"] =
            serde_json::json!(["app", "heap"]);
        document["otherData"]["nxrs"]["complete"] = serde_json::json!(false);
        document["otherData"]["nxrs"]["incomplete_intervals"] = serde_json::json!(4);
        let trace = nxrs_memory_explorer::parse_timing(&document.to_string()).unwrap();
        let first = TimingRange {
            start_ns: 0,
            end_ns: 1_000_000_000,
        };
        let second = TimingRange {
            start_ns: first.end_ns,
            end_ns: 2_000_000_000,
        };
        let stats = trace.stats(first, "");
        assert_eq!(stats.occurrences.interval_starts, 1);
        assert_eq!(stats.occurrences.instants, 0);
        assert_eq!(stats.occurrences.excluded_markers, 4);
        assert_eq!(stats.occurrences.mean_duration_ns(), Some(1_500_000_000.0));
        assert_eq!(stats.rate_denominator.per_second(1), Some(1.0));
        assert_eq!(
            trace
                .stats(first, "outcome:failed")
                .occurrences
                .interval_starts,
            1
        );
        let later = trace.stats(second, "");
        assert_eq!(later.occurrences.interval_starts, 1);
        assert_eq!(later.occurrences.instants, 1);
        assert_eq!(later.occurrences.excluded_markers, 0);
        assert_eq!(later.occurrences.mean_duration_ns(), Some(0.0));

        let sources = trace.source_stats(first, "", 0);
        let calls = sources
            .iter()
            .find(|source| source.name == "malloc")
            .unwrap();
        // The existing allocator table retains intersecting observations;
        // the new half-open start cohort is a separately labelled measure.
        assert_eq!(calls.request_count, 2);
        assert_eq!(calls.requested_bytes, 192);
        assert_eq!(calls.failed_requests, 1);
        assert_eq!(calls.unknown_outcomes, 0);
        assert_eq!(calls.inclusive_ns, 750_000_000);
        assert_eq!(calls.occurrences.interval_starts, 1);
        let orphan = sources
            .iter()
            .find(|source| source.name == "unpaired result")
            .unwrap();
        assert_eq!(orphan.unpaired_results, 1);
        assert_eq!(orphan.request_count, 0);
        assert_eq!(orphan.requested_bytes, 0);
        assert_eq!(orphan.occurrences.excluded_markers, 1);
        let unfinished = sources
            .iter()
            .find(|source| source.name == "unfinished malloc")
            .unwrap();
        assert_eq!(unfinished.request_count, 1);
        assert_eq!(unfinished.requested_bytes, 32);
        assert_eq!(unfinished.unknown_outcomes, 1);
        assert_eq!(unfinished.occurrences.instants, 0);

        let mut view = TimingView::default();
        view.reset(&trace);
        view.mode = TimingMode::Sources;
        view.selection = Some(AddressWindow {
            start: first.start_ns,
            end: first.end_ns,
        });
        view.selected = Some(orphan.representative_event);
        let rendered = rendered_text(&mut view, &trace, Vec2::new(1600.0, 1100.0));
        for expected in [
            "1 complete interval starts · 1.000/s",
            "4 unknown/censored markers excluded",
            "2 intersecting calls",
            "UNKNOWN / CENSORED MARKER",
            "Unknown; only an endpoint was captured",
            "result without paired request",
        ] {
            assert!(
                rendered.contains(expected),
                "Missing {expected:?}: {rendered}"
            );
        }
    }

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

    #[test]
    fn failure_shortcut_selects_known_calls_and_inspector_explains_orphan_results() {
        fn render(
            context: &egui::Context,
            view: &mut TimingView,
            trace: &TimingTrace,
            events: Vec<egui::Event>,
        ) -> egui::FullOutput {
            context.run(
                egui::RawInput {
                    screen_rect: Some(Rect::from_min_size(Pos2::ZERO, Vec2::new(1440.0, 1050.0))),
                    events,
                    ..Default::default()
                },
                |context| view.show(context, trace, egui::Id::new("allocator-call-test")),
            )
        }

        fn visible_text(output: &egui::FullOutput, text: &str) -> Pos2 {
            output
                .shapes
                .iter()
                .find_map(|clipped| {
                    if let egui::epaint::Shape::Text(shape) = &clipped.shape {
                        let bounds = Rect::from_min_size(shape.pos, shape.galley.size());
                        if shape.galley.text() == text && clipped.clip_rect.contains_rect(bounds) {
                            return Some(bounds.center());
                        }
                    }
                    None
                })
                .unwrap_or_else(|| panic!("Missing visible allocator-call text {text:?}"))
        }

        let trace = nxrs_memory_explorer::parse_timing(r#"[
          {"ph":"X","pid":6,"tid":7,"ts":0,"dur":1,"name":"failed malloc","cat":"heap,request","args":{"request_id":"1","request_observed":true,"requested_bytes":4096,"outcome":"failed","result_address":"0x0","failure_reason":"allocator returned NULL"}},
          {"ph":"X","pid":6,"tid":7,"ts":2,"dur":1,"name":"zero-size malloc","cat":"heap,request","args":{"request_id":"2","request_observed":true,"requested_bytes":0,"outcome":"succeeded","failed":false,"result_address":"0x38001000"}},
          {"ph":"i","pid":6,"tid":7,"ts":4,"name":"unfinished malloc","cat":"heap,request","args":{"request_id":"3","request_observed":true,"requested_bytes":128,"incomplete_reason":"missing result"}},
          {"ph":"i","pid":6,"tid":7,"ts":5,"name":"unpaired result","cat":"heap,result","args":{"request_id":"4","request_observed":false,"observed_outcome":"failed","observed_result_address":"0x0","incomplete_reason":"missing request"}}
        ]"#).unwrap();
        let context = egui::Context::default();
        let mut view = TimingView::default();
        view.reset(&trace);
        view.selected = Some(trace.events[0].id);
        render(&context, &mut view, &trace, vec![]);
        let output = render(&context, &mut view, &trace, vec![]);
        visible_text(&output, "FAILURE REASON");
        visible_text(&output, "allocator returned NULL");
        let at = visible_text(&output, "Failed calls (1)");
        for pressed in [true, false] {
            render(
                &context,
                &mut view,
                &trace,
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
        render(&context, &mut view, &trace, vec![]);
        assert_eq!(view.filter, "outcome:failed");
        assert!(matches!(view.mode, TimingMode::Events));
        let selected = &view.analysis.as_ref().unwrap().events;
        assert_eq!(selected.len(), 1);
        assert_eq!(trace.events[selected[0]].name, "failed malloc");

        view.filter = "outcome:unknown".into();
        view.selected = Some(trace.events[3].id);
        let output = render(&context, &mut view, &trace, vec![]);
        visible_text(&output, "result without paired request");
        visible_text(&output, "Unknown; only an endpoint was captured");
        visible_text(&output, "UNPAIRED RETURN OUTCOME");
        visible_text(&output, "missing request");
        assert_eq!(view.analysis.as_ref().unwrap().events.len(), 2);
    }
}
