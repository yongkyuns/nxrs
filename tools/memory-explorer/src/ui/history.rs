//! Cached, per-arena usage history. Point observations never become continuous
//! heap state, and independent heaps/aliases are never added into one budget.

use super::*;
use nxrs_memory_explorer::{
    HeapChunkState, HeapReplay, HeapState, ReplayCoverageKind, ReplayCursor,
};

const USED: Color32 = Color32::from_rgb(215, 127, 156);
const FREE: Color32 = Color32::from_rgb(84, 194, 177);
const LARGEST: Color32 = Color32::from_rgb(100, 161, 235);

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(super) struct Usage {
    pub(super) allocated: u64,
    pub(super) free: u64,
    pub(super) largest: u64,
    pub(super) reserved: u64,
    pub(super) unknown: u64,
    pub(super) allocations: usize,
}

#[derive(Clone, Debug)]
struct Sample {
    time: u64,
    index: usize,
    label: String,
    usage: Usage,
}

#[derive(Clone, Debug)]
struct Series {
    region: String,
    name: String,
    start: u64,
    size: u64,
    samples: Vec<Sample>,
    // Each segment refers to an independently admitted replay point. No
    // segments are generated for snapshots or through loss/mutation intervals.
    segments: Vec<(u64, u64, usize)>,
    gaps: Vec<(u64, u64, bool)>,
}

impl Series {
    fn peak(&self) -> u64 {
        self.samples
            .iter()
            .map(|sample| sample.usage.allocated)
            .max()
            .unwrap_or(0)
    }
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
enum MetricView {
    #[default]
    Allocated,
    Free,
    Largest,
    All,
}

fn plot_values(usage: Usage, metric: MetricView) -> impl Iterator<Item = (u64, Color32)> {
    [
        (usage.allocated, USED),
        (usage.free, FREE),
        (usage.largest, LARGEST),
    ]
    .into_iter()
    .enumerate()
    .filter(move |(index, _)| {
        metric == MetricView::All
            || matches!(
                (metric, index),
                (MetricView::Allocated, 0) | (MetricView::Free, 1) | (MetricView::Largest, 2)
            )
    })
    .map(|(_, value)| value)
}

#[derive(Default)]
pub(super) struct MemoryHistory {
    series: Vec<Series>,
    selected: usize,
    replay: Option<Series>,
    metric: MetricView,
}

impl MemoryHistory {
    pub(super) fn from_model(model: &MemoryModel) -> Self {
        let mut series: Vec<Series> = Vec::new();
        for (index, snapshot) in model.snapshots.iter().enumerate() {
            for region in &model.regions {
                for stats in model.heap_statistics(&region.id, snapshot.time_ns) {
                    let Some(arena) = snapshot.spans.iter().find(|span| span.id == stats.arena)
                    else {
                        continue;
                    };
                    // Address + extent + name identify a chart of the same
                    // observed arena. This is not an allocation lifetime claim.
                    let existing = series.iter().position(|entry| {
                        entry.region == region.id
                            && entry.start == arena.start
                            && entry.size == arena.size
                            && entry.name == arena.name
                    });
                    let target = existing.unwrap_or_else(|| {
                        series.push(Series {
                            region: region.id.clone(),
                            name: arena.name.clone(),
                            start: arena.start,
                            size: arena.size,
                            samples: Vec::new(),
                            segments: Vec::new(),
                            gaps: Vec::new(),
                        });
                        series.len() - 1
                    });
                    series[target].samples.push(Sample {
                        time: snapshot.time_ns,
                        index,
                        label: snapshot.label.clone(),
                        usage: Usage {
                            allocated: stats.allocated_chunk_bytes,
                            free: stats.free_chunk_bytes,
                            largest: stats.largest_free_chunk,
                            reserved: stats.reserved_bytes,
                            unknown: stats.unknown_bytes,
                            allocations: snapshot
                                .spans
                                .iter()
                                .filter(|span| {
                                    span.parent.as_deref() == Some(&arena.id)
                                        && span.kind == SpanKind::Allocation
                                })
                                .count(),
                        },
                    });
                }
            }
        }
        Self {
            series,
            selected: 0,
            replay: None,
            metric: MetricView::default(),
        }
    }

    pub(super) fn install_replay(&mut self, replay: &HeapReplay) {
        let mut series = Series {
            region: replay.journal.heap.region.clone(),
            name: replay.journal.heap.id.clone(),
            start: replay.journal.heap.start,
            size: replay.journal.heap.size,
            samples: Vec::new(),
            segments: Vec::new(),
            gaps: Vec::new(),
        };
        for point in &replay.points {
            if let HeapState::Known(state) =
                replay.heap_state_at(&replay.journal.heap.id, ReplayCursor::Point(point.id))
            {
                let mut usage = Usage::default();
                for chunk in state.chunks {
                    match chunk.state {
                        HeapChunkState::Allocated => {
                            usage.allocated += chunk.size;
                            usage.allocations += 1;
                        }
                        HeapChunkState::Free => {
                            usage.free += chunk.size;
                            usage.largest = usage.largest.max(chunk.size);
                        }
                        HeapChunkState::Reserved => usage.reserved += chunk.size,
                    }
                }
                series.samples.push(Sample {
                    time: point.time_ns,
                    index: point.id,
                    label: format!("#{} · {}", point.sequence, point.label),
                    usage,
                });
            }
        }
        for coverage in &replay.coverage {
            match coverage.kind {
                ReplayCoverageKind::Reconstructed => {
                    if let Some(id) = coverage.point_id {
                        series
                            .segments
                            .push((coverage.range.start_ns, coverage.range.end_ns, id));
                    }
                }
                ReplayCoverageKind::Mutation => {
                    series
                        .gaps
                        .push((coverage.range.start_ns, coverage.range.end_ns, false))
                }
                ReplayCoverageKind::Unknown(_) => {
                    series
                        .gaps
                        .push((coverage.range.start_ns, coverage.range.end_ns, true))
                }
            }
        }
        self.replay = Some(series);
    }
}

/// Separate integer subtraction keeps high source-clock values precise. Legacy
/// observation coordinates use index spacing; their magnitudes are not time.
fn point_fraction(sample: &Sample, measured: bool, range: AddressWindow, count: usize) -> f64 {
    if measured {
        range.fraction(sample.time)
    } else {
        sample.index as f64 / count.saturating_sub(1).max(1) as f64
    }
}

struct UsagePlot {
    range: AddressWindow,
    count: usize,
    measured: bool,
    selected_time: Option<u64>,
    selected_point: Option<usize>,
    height: f32,
    metric: MetricView,
}

fn draw_usage(ui: &mut egui::Ui, series: &Series, options: UsagePlot) -> Option<Option<u64>> {
    let UsagePlot {
        range,
        count,
        measured,
        selected_time,
        selected_point,
        height,
        metric,
    } = options;
    let (rect, response) = ui.allocate_exact_size(
        Vec2::new(ui.available_width(), height),
        Sense::click_and_drag(),
    );
    let plot = Rect::from_min_max(
        rect.min + Vec2::new(70.0, 7.0),
        rect.max - Vec2::new(12.0, 25.0),
    );
    if plot.width() <= 0.0 || plot.height() <= 0.0 {
        return None;
    }
    let max_bytes = series
        .samples
        .iter()
        .flat_map(|sample| plot_values(sample.usage, metric).map(|(bytes, _)| bytes))
        .max()
        .unwrap_or(series.size)
        .max(1);
    let at = |fraction: f64| plot.left() + fraction as f32 * plot.width();
    let y = |bytes: u64| plot.bottom() - (bytes as f64 / max_bytes as f64) as f32 * plot.height();
    let painter = ui.painter().with_clip_rect(rect);
    painter.rect_filled(plot, 3, BACKGROUND);
    for (fraction, value) in [(0.0, 0), (0.5, max_bytes / 2), (1.0, max_bytes)] {
        let axis_y = plot.bottom() - fraction * plot.height();
        painter.line_segment(
            [
                Pos2::new(plot.left(), axis_y),
                Pos2::new(plot.right(), axis_y),
            ],
            Stroke::new(1.0, LINE),
        );
        painter.text(
            Pos2::new(plot.left() - 8.0, axis_y),
            Align2::RIGHT_CENTER,
            format_bytes(value),
            FontId::monospace(9.0),
            MUTED,
        );
    }
    for (start, end, lost) in &series.gaps {
        let gap = Rect::from_min_max(
            Pos2::new(at(range.fraction(*start)), plot.top()),
            Pos2::new(at(range.fraction(*end)), plot.bottom()),
        )
        .intersect(plot);
        if gap.is_positive() {
            let color = if *lost {
                Color32::from_rgb(220, 112, 121)
            } else {
                WARNING
            };
            painter.rect_filled(gap, 0, color.gamma_multiply(0.13));
            hatch(
                &painter.with_clip_rect(gap),
                gap,
                color.gamma_multiply(0.22),
            );
        }
    }
    for (start, end, id) in &series.segments {
        if let Ok(index) = series
            .samples
            .binary_search_by_key(id, |sample| sample.index)
        {
            let sample = &series.samples[index];
            for (bytes, color) in plot_values(sample.usage, metric) {
                painter.line_segment(
                    [
                        Pos2::new(at(range.fraction(*start)), y(bytes)),
                        Pos2::new(at(range.fraction(*end)), y(bytes)),
                    ],
                    Stroke::new(1.8, color),
                );
            }
        }
    }
    let hovered = response
        .hover_pos()
        .filter(|pointer| plot.expand(8.0).contains(*pointer))
        .and_then(|pointer| {
            series
                .samples
                .iter()
                .min_by(|a, b| {
                    (at(point_fraction(a, measured, range, count)) - pointer.x)
                        .abs()
                        .total_cmp(
                            &(at(point_fraction(b, measured, range, count)) - pointer.x).abs(),
                        )
                })
                .filter(|sample| {
                    (at(point_fraction(sample, measured, range, count)) - pointer.x).abs() < 12.0
                })
        });
    for sample in &series.samples {
        let x = at(point_fraction(sample, measured, range, count));
        let selected = if let Some(id) = selected_point {
            sample.index == id
        } else {
            selected_time == Some(sample.time)
        };
        if selected {
            painter.line_segment(
                [Pos2::new(x, plot.top()), Pos2::new(x, plot.bottom())],
                Stroke::new(1.0, TEXT.gamma_multiply(0.65)),
            );
        }
        for (bytes, color) in plot_values(sample.usage, metric) {
            painter.circle_filled(
                Pos2::new(x, y(bytes)),
                if selected { 4.2 } else { 2.5 },
                color,
            );
        }
        if sample.usage.unknown > 0 {
            painter.text(
                Pos2::new(x, plot.top()),
                Align2::CENTER_TOP,
                "?",
                FontId::proportional(11.0),
                WARNING,
            );
        }
    }
    let axis = if measured {
        format!(
            "{:.3} ms source-clock elapsed",
            range.span() as f64 / 1_000_000.0
        )
    } else {
        format!("Observation {} · elapsed time unavailable", count.max(1))
    };
    painter.text(
        Pos2::new(plot.left(), plot.bottom() + 9.0),
        Align2::LEFT_TOP,
        if measured { "0 ms" } else { "Observation 1" },
        FontId::proportional(10.0),
        MUTED,
    );
    painter.text(
        Pos2::new(plot.right(), plot.bottom() + 9.0),
        Align2::RIGHT_TOP,
        axis,
        FontId::proportional(10.0),
        MUTED,
    );
    if let Some(sample) = hovered {
        response.clone().on_hover_ui(|ui| {
            ui.strong(&sample.label);
            ui.label(format!(
                "Allocated {} · {} chunks",
                format_bytes(sample.usage.allocated),
                sample.usage.allocations
            ));
            ui.label(format!(
                "Free {} · largest {}",
                format_bytes(sample.usage.free),
                format_bytes(sample.usage.largest)
            ));
            ui.label(format!(
                "Reserved {} · unobserved {}",
                format_bytes(sample.usage.reserved),
                format_bytes(sample.usage.unknown)
            ));
            if measured {
                ui.label(format!("Source {} ns", sample.time));
            }
            ui.label("Click to inspect memory at this observation.");
        });
    }
    let pointer = response
        .interact_pointer_pos()
        .filter(|_| response.clicked() || response.dragged())?;
    let fraction = ((pointer.x - plot.left()) / plot.width()).clamp(0.0, 1.0) as f64;
    if series.segments.is_empty() && series.gaps.is_empty() {
        // A point click can select a snapshot. The spaces between retain their
        // unknown-state semantics, even when the observation axis is ordinal.
        if let Some(sample) = series
            .samples
            .iter()
            .min_by(|a, b| {
                (point_fraction(a, measured, range, count) - fraction)
                    .abs()
                    .total_cmp(&(point_fraction(b, measured, range, count) - fraction).abs())
            })
            .filter(|sample| {
                ((point_fraction(sample, measured, range, count) - fraction) as f32 * plot.width())
                    .abs()
                    < 10.0
            })
        {
            return Some(Some(sample.time));
        }
    }
    if !measured {
        return Some(None);
    }
    let time = range.at_fraction(fraction);
    if series.segments.is_empty()
        && series.gaps.is_empty()
        && series.samples.iter().any(|sample| sample.time == time)
    {
        // Rounding a subpixel gap onto an observed timestamp must not turn a
        // click away from a point into fabricated observation coverage.
        return Some(None);
    }
    Some(Some(time))
}

fn usage_legend(ui: &mut egui::Ui, metric: &mut MetricView) {
    ui.horizontal_wrapped(|ui| {
        ui.label(RichText::new("Plot").small().color(MUTED));
        for (value, label, color) in [
            (MetricView::Allocated, "Allocated", USED),
            (MetricView::Free, "Free", FREE),
            (MetricView::Largest, "Largest free", LARGEST),
            (MetricView::All, "All", TEXT),
        ] {
            ui.selectable_value(metric, value, RichText::new(label).small().color(color));
        }
        ui.label(
            RichText::new("Raw chunk bytes include allocator overhead")
                .small()
                .color(MUTED),
        );
    });
}

fn current_usage(ui: &mut egui::Ui, usage: Option<Usage>, peak: u64) {
    ui.horizontal_wrapped(|ui| {
        if let Some(value) = usage {
            ui.label(RichText::new(format!("Now {} allocated · {} chunks", format_bytes(value.allocated), value.allocations)).small().color(USED));
            ui.label(RichText::new(format!("{} free · largest {}", format_bytes(value.free), format_bytes(value.largest))).small().color(FREE));
            if value.unknown > 0 { ui.label(RichText::new(format!("{} unobserved", format_bytes(value.unknown))).small().color(WARNING)); }
        } else { ui.label(RichText::new("Memory at cursor: unknown").small().color(WARNING)); }
        ui.label(RichText::new(format!("Observed allocation peak {}", format_bytes(peak))).small().color(MUTED))
            .on_hover_text("Highest allocated chunk extent in the admitted observations. A point capture can miss higher intervening peaks.");
    });
}

impl MemoryExplorer {
    pub(super) fn memory_history_timeline(&mut self, ctx: &egui::Context) {
        let has_observations = self
            .model
            .as_ref()
            .is_some_and(|model| !model.snapshots.is_empty());
        egui::TopBottomPanel::bottom(self.widget_id("memory-usage-history"))
            .resizable(true).default_height(if has_observations { 254.0 } else { 100.0 }).min_height(90.0)
            .frame(egui::Frame::new().fill(PANEL).inner_margin(egui::Margin::symmetric(20, 10)))
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    eyebrow(ui, "MEMORY USAGE OVER TIME");
                    badge(ui, if self.exact_snapshot() { "OBSERVED POINT" } else { "RUNTIME STATE UNKNOWN" }, if self.exact_snapshot() { ACCENT } else { WARNING });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.add_enabled(has_observations, egui::Button::new("Next  ›")).clicked() { self.adjacent_snapshot(true); }
                        if ui.add_enabled(has_observations, egui::Button::new("‹  Previous")).clicked() { self.adjacent_snapshot(false); }
                    });
                });
                let Some(model) = &self.model else { return; };
                if model.snapshots.is_empty() {
                    ui.label("This ELF describes placement; runtime memory has not been captured.");
                    ui.label(RichText::new("Enable [build] diagnostics = true, then load heap/stack snapshots or a heap journal. Native heap-usage counters are also visible in Timing.").small().color(MUTED));
                    return;
                }
                let count = model.snapshots.len();
                let first = model.snapshots[0].time_ns;
                let last = model.snapshots[count-1].time_ns;
                let range = AddressWindow { start: first, end: last.max(first.saturating_add(1)) };
                let measured = self.correlation.is_some();
                let selected_label = self.history.series.get(self.history.selected).map(|series| format!("{} · {}", series.name, format_bytes(series.size)))
                    .unwrap_or_else(|| "No heap geometry captured".into());
                let mut open_heap = false;
                ui.horizontal_wrapped(|ui| {
                    egui::ComboBox::from_id_salt("history-arena").selected_text(selected_label).width(280.0).show_ui(ui, |ui| {
                        for (index, series) in self.history.series.iter().enumerate() {
                            ui.selectable_value(&mut self.history.selected, index, format!("{} · {} · {:#x}", series.name, format_bytes(series.size), series.start));
                        }
                    });
                    open_heap = ui.add_enabled(!self.history.series.is_empty(), egui::Button::new("Inspect heap")).clicked();
                    ui.label(RichText::new("One arena at a time; heaps, pools and stack overlays are not added together.").small().color(MUTED));
                });
                if let Some(series) = self.history.series.get(self.history.selected) {
                    let usage = self.time_ns.and_then(|time| series.samples.iter().find(|sample| sample.time == time)).map(|sample| sample.usage);
                    current_usage(ui, usage, series.peak());
                    usage_legend(ui, &mut self.history.metric);
                    if let Some(time) = draw_usage(ui, series, UsagePlot { range, count, measured, selected_time: self.time_ns, selected_point: None, height: (ui.available_height()-36.0).max(80.0), metric: self.history.metric }) {
                        self.time_ns = time;
                        self.selected = None;
                    }
                } else {
                    ui.label(RichText::new("Snapshots contain no allocator arenas. Select observations below to inspect captured task stacks.").small().color(MUTED));
                }
                let snapshots: Vec<_> = model.snapshots.iter().map(|snapshot| (snapshot.time_ns, snapshot.label.clone())).collect();
                let label = snapshots.iter().find(|snapshot| Some(snapshot.0) == self.time_ns).map(|snapshot| snapshot.1.as_str()).unwrap_or("Between observations");
                let mut seek = None;
                let mut timing_seek = None;
                ui.horizontal_wrapped(|ui| {
                    egui::ComboBox::from_id_salt("memory-observation").selected_text(label).width(280.0).show_ui(ui, |ui| {
                        for (index, (time, label)) in snapshots.iter().enumerate() {
                            if ui.selectable_label(self.time_ns == Some(*time), format!("{} · {label}", index+1)).clicked() { seek = Some(*time); }
                        }
                    });
                    if let Some(link) = self.correlation.as_ref().and_then(|correlation| self.time_ns.and_then(|time| correlation.link_for_snapshot(time))) {
                        if ui.small_button("Show in timing").clicked() { timing_seek = Some(link.trace_time_ns); }
                    }
                    ui.label(RichText::new(if measured { "Recorded clock · dots are observations; gaps are unknown" }
                        else { "Observation order · elapsed time and between-point usage are unknown" }).small().color(MUTED));
                });
                if let Some(time) = seek { self.time_ns = Some(time); self.selected = None; }
                if let Some(time) = timing_seek { self.open_timing_at(time); }
                if open_heap { self.focus_history_arena(); }
            });
    }

    fn focus_history_arena(&mut self) {
        let Some(series) = self.history.series.get(self.history.selected) else {
            return;
        };
        let key = series.region.clone();
        let range = AddressWindow {
            start: series.start,
            end: series.start.saturating_add(series.size),
        };
        self.physical = false;
        if let Some(region) = self.regions().iter().find(|region| region.key == key) {
            self.select_region(region);
            self.viewport = range.fit_inside(region.bounds);
            self.view = ViewMode::Memory;
        }
    }

    pub(super) fn replay_usage_chart(&mut self, ui: &mut egui::Ui, height: f32) -> Option<u64> {
        if self.history.replay.is_none() {
            if let Some(replay) = &self.replay {
                self.history.install_replay(replay);
            }
        }
        let replay = self.replay.as_ref()?;
        let series = self.history.replay.as_ref()?;
        let state = self.replay_cursor.and_then(|cursor| {
            match replay.heap_state_at(&replay.journal.heap.id, cursor) {
                HeapState::Known(state) => series
                    .samples
                    .iter()
                    .find(|sample| sample.index == state.point_id),
                HeapState::Unknown(_) => None,
            }
        });
        current_usage(ui, state.map(|sample| sample.usage), series.peak());
        usage_legend(ui, &mut self.history.metric);
        draw_usage(
            ui,
            series,
            UsagePlot {
                range: AddressWindow {
                    start: replay.bounds.start_ns,
                    end: replay.bounds.end_ns,
                },
                count: replay.points.len(),
                measured: true,
                selected_time: self.time_ns,
                selected_point: state.map(|sample| sample.index),
                height,
                metric: self.history.metric,
            },
        )
        .flatten()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heap_usage_excludes_static_sections_stack_overlays_and_allocator_containers() {
        let model = nxrs_memory_explorer::demo();
        let history = MemoryHistory::from_model(&model);
        assert_eq!(history.series.len(), 1);
        let series = &history.series[0];
        for sample in &series.samples {
            let snapshot = &model.snapshots[sample.index];
            let allocations: Vec<_> = snapshot
                .spans
                .iter()
                .filter(|span| span.kind == SpanKind::Allocation)
                .collect();
            assert_eq!(
                sample.usage.allocated,
                allocations.iter().map(|span| span.size).sum::<u64>()
            );
            assert_eq!(sample.usage.allocations, allocations.len());
            assert_eq!(
                sample.usage.allocated
                    + sample.usage.free
                    + sample.usage.reserved
                    + sample.usage.unknown,
                series.size
            );
        }
        assert!(
            series.segments.is_empty(),
            "point captures cannot establish a held state"
        );
    }

    #[test]
    fn uncaptured_bytes_remain_unknown_in_usage_history() {
        let mut model = nxrs_memory_explorer::demo();
        let missing = model.snapshots[0]
            .spans
            .iter()
            .find(|span| span.kind == SpanKind::Free)
            .unwrap()
            .clone();
        model.snapshots[0]
            .spans
            .retain(|span| span.id != missing.id);
        model.snapshots[0].complete_regions.clear();
        let history = MemoryHistory::from_model(&model);
        assert_eq!(history.series[0].samples[0].usage.unknown, missing.size);
    }

    #[test]
    fn ordinal_history_does_not_imply_elapsed_time_and_large_clock_values_stay_precise() {
        let sample = Sample {
            time: u64::MAX - 8,
            index: 1,
            label: "middle".into(),
            usage: Usage::default(),
        };
        let range = AddressWindow {
            start: u64::MAX - 10,
            end: u64::MAX,
        };
        assert_eq!(point_fraction(&sample, false, range, 3), 0.5);
        assert_eq!(point_fraction(&sample, true, range, 3), 0.2);
    }

    #[test]
    fn selecting_a_missing_observation_does_not_hold_last_heap_usage() {
        let model = nxrs_memory_explorer::demo();
        let history = MemoryHistory::from_model(&model);
        let series = &history.series[0];
        let first = series.samples[0].time;
        let next = series.samples[1].time;
        let between = first + (next - first) / 2;
        assert!(series.samples.iter().all(|sample| sample.time != between));
        assert!(series.segments.is_empty());
    }
    #[test]
    fn replay_history_keeps_mutation_and_loss_gaps_until_checkpoint_recovery() {
        use serde_json::json;
        let model = nxrs_memory_explorer::demo();
        let start = 0x2000_0800u64;
        let free = json!({"start":start,"size":256,"state":"free"});
        let journal = json!({
            "schema_version":1,"build":model.build,
            "provenance":{"nuttx_revision":"a".repeat(40),"config_sha256":"b".repeat(64),"collector":"history-regression"},
            "recording_id":"history-test","clock":"test-clock",
            "heap":{"id":"history-heap","epoch":"one","region":"demo-ram","start":start,"size":256},
            "initial":{"sequence":0,"time_ns":0,"chunks":[free.clone()]},
            "records":[
                {"kind":"transaction","sequence":1,"heap_epoch":"one","start_ns":2,"commit_ns":4,"start":start,"size":256,
                 "expected":[free.clone()],"replacement":[{"start":start,"size":64,"state":"allocated"},{"start":start+64,"size":192,"state":"free"}],"label":"allocate 64"},
                {"kind":"loss","heap_epoch":"one","time_ns":8,"reason":"missing transaction"},
                {"kind":"checkpoint","heap_epoch":"one","sequence":3,"time_ns":10,"chunks":[free],"label":"recovered"}
            ],"closing":{"heap_epoch":"one","sequence":3,"time_ns":12}
        });
        let replay =
            nxrs_memory_explorer::parse_heap_journal(&journal.to_string(), &model, None).unwrap();
        let mut history = MemoryHistory::from_model(&model);
        history.install_replay(&replay);
        let series = history.replay.as_ref().unwrap();
        assert_eq!(
            series
                .samples
                .iter()
                .map(|sample| sample.usage.allocated)
                .collect::<Vec<_>>(),
            [0, 64, 0]
        );
        assert_eq!(series.peak(), 64);
        for time in [2, 3, 5, 8, 9] {
            assert!(
                !series
                    .segments
                    .iter()
                    .any(|(start, end, _)| *start <= time && time < *end),
                "unknown interval plotted as usage at {time}"
            );
        }
        assert!(series
            .segments
            .iter()
            .any(|(start, end, _)| *start == 10 && *end == 12));
        assert!(series
            .gaps
            .iter()
            .any(|(start, end, lost)| *lost && *start <= 8 && *end > 8));
    }

    #[test]
    fn history_heap_navigation_opens_the_arena_without_changing_observation() {
        let mut app = MemoryExplorer::new(InputPaths::default(), true, None).unwrap();
        let before = app.time_ns;
        app.focus_history_arena();
        assert_eq!(app.view, ViewMode::Memory);
        assert_eq!(app.region, "demo-ram");
        assert_eq!(app.time_ns, before);
        let series = &app.history.series[app.history.selected];
        assert_eq!(app.viewport.start, series.start);
        assert_eq!(app.viewport.span(), series.size);
    }
}
