//! Heap-only evidence navigation. A replay cursor never becomes a synthetic
//! Snapshot, and never exposes stacks or another heap's last observation.

use super::*;
use nxrs_memory_explorer::{
    HeapEvidence, HeapState, HeapUnknownReason, ReplayCoverageKind, ReplayCursor,
};

const LOST: Color32 = Color32::from_rgb(220, 112, 121);

impl MemoryExplorer {
    pub(super) fn install_replay(&mut self) -> Result<(), String> {
        let model = self
            .model
            .as_ref()
            .ok_or("A heap journal needs its final ELF.")?;
        let replay = nxrs_memory_explorer::load_heap_journal(
            Path::new(self.inputs.heap_journal.trim()),
            model,
            self.trace.as_ref(),
        )
        .map_err(|error| error.to_string())?;
        let heap_region = replay.journal.heap.region.clone();
        let heap_start = replay.journal.heap.start;
        let heap_size = replay.journal.heap.size;
        self.replay = Some(replay);
        self.replay_enabled = true;
        self.select_replay_cursor(ReplayCursor::Point(0));
        if let Some(region) = self
            .regions()
            .iter()
            .find(|r| r.members.contains(&heap_region))
        {
            self.select_region(region);
            self.viewport = AddressWindow {
                start: heap_start,
                end: heap_start + heap_size,
            }
            .fit_inside(region.bounds);
        }
        self.view = ViewMode::Memory;
        self.status = "Heap journal loaded. Select a sequence or source-clock time; other runtime layers remain unknown.".into();
        Ok(())
    }

    pub(super) fn replay_active(&self) -> bool {
        self.replay_enabled && self.replay.is_some()
    }

    fn replay_state(&self) -> Option<HeapState<'_>> {
        let replay = self.replay.as_ref().filter(|_| self.replay_active())?;
        Some(replay.heap_state_at(&replay.journal.heap.id, self.replay_cursor?))
    }

    pub(super) fn runtime_spans(&self) -> Vec<Span> {
        if self.replay_active() {
            return match self.replay_state() {
                Some(HeapState::Known(state)) => state.spans(),
                _ => Vec::new(),
            };
        }
        self.model
            .as_ref()
            .and_then(|model| self.time_ns.and_then(|time| model.snapshot_at(time)))
            .map(|snapshot| snapshot.spans.clone())
            .unwrap_or_default()
    }

    pub(super) fn runtime_lane_known(&self, lane: usize) -> bool {
        if !self.replay_active() {
            return self.exact_snapshot();
        }
        lane == 2
            && self.current_region().is_some_and(|region| {
                matches!(self.replay_state(), Some(HeapState::Known(state))
                if region.members.contains(&state.heap.region))
            })
    }

    fn select_replay_cursor(&mut self, cursor: ReplayCursor) {
        let Some(replay) = &self.replay else {
            return;
        };
        let time = match cursor {
            ReplayCursor::Time(time) => Some(time),
            ReplayCursor::Point(id) => replay.points.get(id).map(|point| point.time_ns),
            ReplayCursor::Sequence(_) => {
                match replay.heap_state_at(&replay.journal.heap.id, cursor) {
                    HeapState::Known(state) => Some(state.time_ns),
                    HeapState::Unknown(_) => None,
                }
            }
        };
        self.replay_cursor = Some(cursor);
        self.time_ns = time;
        self.replay_time_entry = time.map(|time| time.to_string()).unwrap_or_default();
        self.selected = None;
        self.timing.clear_cursor();
        if let (Some(time), Some(trace)) = (time, &self.trace) {
            if let Some(relative) = trace
                .metadata
                .origin_ns
                .and_then(|origin| time.checked_sub(origin))
            {
                if trace.bounds.contains(relative) {
                    self.timing.seek_time(relative, trace);
                }
            }
        }
    }

    fn replay_point_index(&self) -> Option<usize> {
        match self.replay_state()? {
            HeapState::Known(state) => Some(state.point_id),
            HeapState::Unknown(_) => None,
        }
    }

    fn adjacent_replay_index(&self, next: bool) -> Option<usize> {
        let replay = self.replay.as_ref()?;
        if let Some(ReplayCursor::Point(id)) = self.replay_cursor {
            return if next {
                id.checked_add(1).filter(|id| *id < replay.points.len())
            } else {
                id.checked_sub(1)
            };
        }
        let time = self.time_ns?;
        if next {
            replay
                .points
                .iter()
                .find(|point| point.time_ns > time)
                .map(|point| point.id)
        } else {
            replay
                .points
                .iter()
                .rev()
                .find(|point| point.time_ns < time)
                .map(|point| point.id)
        }
    }

    pub(super) fn adjacent_replay_point(&mut self, next: bool) {
        if let Some(id) = self.adjacent_replay_index(next) {
            self.select_replay_cursor(ReplayCursor::Point(id));
        }
    }

    pub(super) fn replay_mode_controls(&mut self, ui: &mut egui::Ui) {
        if self.replay.is_none() {
            return;
        }
        let mut enabled = self.replay_enabled;
        ui.horizontal(|ui| {
            ui.label(RichText::new("Memory evidence").small().color(MUTED));
            ui.selectable_value(&mut enabled, false, "Point snapshots");
            ui.selectable_value(&mut enabled, true, "Heap replay");
        });
        if enabled != self.replay_enabled {
            self.replay_enabled = enabled;
            self.selected = None;
            self.comparison = None;
            if enabled {
                self.select_replay_cursor(self.replay_cursor.unwrap_or(ReplayCursor::Point(0)));
            } else {
                self.time_ns = self
                    .model
                    .as_ref()
                    .and_then(|model| model.snapshots.first())
                    .map(|snapshot| snapshot.time_ns);
                if self.view == ViewMode::Timing {
                    self.sync_timing_cursor();
                }
            }
        }
    }

    pub(super) fn open_replay_timing(&mut self) {
        if !self.replay_time_is_covered() {
            self.error = Some("The selected heap time is outside the imported trace. No timing cursor is available there.".into());
            return;
        }
        if let Some(cursor) = self.replay_cursor {
            self.select_replay_cursor(cursor);
        }
        if self.trace.is_some() {
            self.view = ViewMode::Timing;
        }
    }

    pub(super) fn replay_time_is_covered(&self) -> bool {
        self.trace
            .as_ref()
            .zip(self.time_ns)
            .is_some_and(|(trace, time)| {
                trace
                    .metadata
                    .origin_ns
                    .and_then(|origin| time.checked_sub(origin))
                    .is_some_and(|relative| trace.bounds.contains(relative))
            })
    }

    pub(super) fn sync_replay_timing_cursor(&mut self) {
        let source = self
            .trace
            .as_ref()
            .and_then(|trace| trace.metadata.origin_ns)
            .zip(self.timing.cursor())
            .and_then(|(origin, time)| origin.checked_add(time));
        if let Some(source) = source {
            self.replay_cursor = Some(ReplayCursor::Time(source));
            self.time_ns = Some(source);
            self.replay_time_entry = source.to_string();
            self.selected = None;
        }
    }

    pub(super) fn replay_inspector(&self, ui: &mut egui::Ui) {
        let Some(HeapState::Known(state)) = self.replay_state() else {
            return;
        };
        field(ui, "EVIDENCE", evidence_label(state.evidence));
        field(ui, "COMMITTED SEQUENCE", &state.sequence.to_string());
        field(ui, "SOURCE CLOCK", &format!("{} ns", state.time_ns));
        field(
            ui,
            "STATE BOUNDARY",
            &format!("{} ns", state.boundary_time_ns),
        );
        ui.label(RichText::new("Chunk geometry only. Allocation lifetime, requested size, and source ownership are unknown.")
            .small().color(WARNING));
    }

    pub(super) fn replay_timeline(&mut self, ctx: &egui::Context) {
        let Some(replay) = &self.replay else {
            return;
        };
        let points = replay.points.clone();
        let coverage = replay.coverage.clone();
        let bounds = AddressWindow {
            start: replay.bounds.start_ns,
            end: replay.bounds.end_ns,
        };
        let heap = replay.journal.heap.id.clone();
        let recording_label = replay.journal.build.label.clone();
        let clock = replay.journal.clock.clone();
        let warnings = replay.warnings.clone();
        let selected_point = self.replay_point_index();
        let (label, detail, color) = match self.replay_state() {
            Some(HeapState::Known(state)) => (
                evidence_label(state.evidence),
                format!(
                    "{} · sequence {} · {} chunks · source {} ns",
                    heap,
                    state.sequence,
                    state.chunks.len(),
                    state.time_ns
                ),
                ACCENT,
            ),
            Some(HeapState::Unknown(state)) => (
                if state.reason == HeapUnknownReason::MutationInProgress {
                    "MUTATION IN PROGRESS"
                } else {
                    "HEAP STATE UNKNOWN"
                },
                format!("{} · {} · {}", heap, state.reason, state.detail),
                if state.reason == HeapUnknownReason::MutationInProgress {
                    WARNING
                } else {
                    LOST
                },
            ),
            None => (
                "HEAP STATE UNKNOWN",
                "Select a journal sequence or source time.".into(),
                WARNING,
            ),
        };
        let previous = self.adjacent_replay_index(false);
        let next = self.adjacent_replay_index(true);
        let mut seek = None;
        let mut open_memory = false;
        let mut open_timing = false;
        egui::TopBottomPanel::bottom("heap-replay-timeline")
            .exact_height(224.0)
            .frame(egui::Frame::new().fill(PANEL).inner_margin(egui::Margin::symmetric(20, 10)))
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    eyebrow(ui, "HEAP REPLAY");
                    badge(ui, label, color);
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if self.view == ViewMode::Timing {
                            open_memory = ui.button("Open heap memory").clicked();
                        } else if self.trace.is_some() {
                            open_timing = ui.add_enabled(self.replay_time_is_covered(), egui::Button::new("Show in timing")).clicked();
                        }
                        if ui.add_enabled(next.is_some(), egui::Button::new("Next  ›")).clicked() {
                            seek = next.map(ReplayCursor::Point);
                        }
                        if ui.add_enabled(previous.is_some(), egui::Button::new("‹  Previous")).clicked() {
                            seek = previous.map(ReplayCursor::Point);
                        }
                    });
                });
                if !recording_label.is_empty() {
                    ui.label(RichText::new(&recording_label).small().color(MUTED));
                }
                let (rect, response) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 57.0), Sense::click_and_drag());
                let plot = rect.shrink2(Vec2::new(5.0, 0.0));
                let y = plot.top() + 13.0;
                let at = |time| plot.left() + bounds.fraction(time) as f32 * plot.width();
                let painter = ui.painter();
                let band = Rect::from_min_max(Pos2::new(plot.left(), y), Pos2::new(plot.right(), y + 13.0));
                painter.rect_filled(band, 2, SURFACE);
                for segment in &coverage {
                    let segment_color = match segment.kind {
                        ReplayCoverageKind::Reconstructed => ACCENT,
                        ReplayCoverageKind::Mutation => WARNING,
                        ReplayCoverageKind::Unknown(_) => LOST,
                    };
                    let range = Rect::from_min_max(
                        Pos2::new(at(segment.range.start_ns), y), Pos2::new(at(segment.range.end_ns), y + 13.0),
                    ).intersect(band);
                    if range.is_positive() { painter.rect_filled(range, 0, segment_color.gamma_multiply(0.45)); }
                }
                for point in &points {
                    let x = at(point.time_ns);
                    let point_color = if point.evidence == HeapEvidence::ObservedCheckpoint { Color32::WHITE } else { ACCENT };
                    painter.line_segment([Pos2::new(x, y - 3.0), Pos2::new(x, y + 16.0)], Stroke::new(1.0, point_color));
                    if selected_point == Some(point.id) {
                        painter.circle_stroke(Pos2::new(x, y + 6.5), 8.0, Stroke::new(1.5, Color32::WHITE));
                    }
                }
                if let Some(time) = self.time_ns.filter(|time| *time >= bounds.start && *time <= bounds.end) {
                    let x = at(time);
                    painter.line_segment([Pos2::new(x, y - 8.0), Pos2::new(x, y + 20.0)], Stroke::new(2.0, color));
                }
                painter.text(Pos2::new(plot.left(), y + 24.0), Align2::LEFT_TOP,
                    format!("{} ns", bounds.start), FontId::monospace(10.0), MUTED);
                painter.text(Pos2::new(plot.right(), y + 24.0), Align2::RIGHT_TOP,
                    format!("{} ns", bounds.end), FontId::monospace(10.0), MUTED);
                if let Some(pointer) = response.interact_pointer_pos().filter(|_| response.clicked() || response.dragged()) {
                    // The axis selects a clock coordinate, never silently chooses
                    // one of multiple commits in the same tick.
                    seek = Some(ReplayCursor::Time(bounds.at_fraction(((pointer.x - plot.left()) / plot.width()) as f64)));
                }
                ui.horizontal(|ui| {
                    let selected = match self.replay_cursor {
                        Some(ReplayCursor::Point(id)) => points.get(id).map(|point| format!("#{} · {}", point.sequence, abbreviated(&point.label, 36))),
                        _ => None,
                    }.unwrap_or_else(|| "Time cursor · choose an exact sequence".into());
                    egui::ComboBox::from_id_salt("heap-sequence")
                        .selected_text(selected).width(370.0).show_ui(ui, |ui| {
                            for point in &points {
                                if ui.selectable_label(self.replay_cursor == Some(ReplayCursor::Point(point.id)),
                                    format!("#{} · {} ns · {}", point.sequence, point.time_ns, point.label)).clicked() {
                                    seek = Some(ReplayCursor::Point(point.id));
                                }
                            }
                        });
                    ui.label(RichText::new("Source ns").small().color(MUTED));
                    let entry = ui.add(egui::TextEdit::singleline(&mut self.replay_time_entry).desired_width(155.0));
                    if ui.small_button("Go").clicked() || (entry.lost_focus() && ui.input(|input| input.key_pressed(egui::Key::Enter))) {
                        match self.replay_time_entry.trim().parse::<u64>() {
                            Ok(time) => { seek = Some(ReplayCursor::Time(time)); self.error = None; }
                            Err(_) => self.error = Some("Source time requires an unsigned integer number of nanoseconds.".into()),
                        }
                    }
                });
                ui.label(RichText::new(detail).small().color(color));
                if self.view == ViewMode::Timing && !self.replay_time_is_covered() {
                    ui.label(RichText::new("Selected source time is outside this trace; its timing cursor is cleared.").small().color(WARNING));
                }
                ui.horizontal_wrapped(|ui| {
                    ui.label(RichText::new("■ Reconstructed").small().color(ACCENT));
                    ui.label(RichText::new("■ Mutation: unknown").small().color(WARNING));
                    ui.label(RichText::new("■ Missing evidence").small().color(LOST));
                    ui.label(RichText::new("│ Observed checkpoint").small().color(Color32::WHITE));
                    ui.label(RichText::new(format!("Clock: {clock}")).small().color(MUTED));
                });
                let note = warnings.first().map(String::as_str).unwrap_or("Sequence preserves order within a tick. Coverage depends on a qualified complete collector; stacks and allocation lifetimes remain unknown.");
                ui.label(RichText::new(note).small().color(MUTED));
            });
        if let Some(cursor) = seek {
            self.select_replay_cursor(cursor);
        }
        if open_memory {
            self.view = ViewMode::Memory;
        }
        if open_timing {
            self.open_replay_timing();
        }
    }

    pub(super) fn smoke_replay_navigation(&mut self) -> Result<(), String> {
        let replay = self.replay.as_ref().ok_or("No heap journal loaded")?;
        let target = replay
            .points
            .iter()
            .find(|point| point.evidence == HeapEvidence::Reconstructed)
            .ok_or("Replay smoke needs a valid transaction")?
            .id;
        let unknown_time = replay
            .coverage
            .iter()
            .find(|segment| {
                segment.kind == ReplayCoverageKind::Mutation
                    && segment.range.end_ns > segment.range.start_ns
            })
            .map(|segment| segment.range.start_ns);
        self.select_replay_cursor(ReplayCursor::Point(target));
        if self.runtime_spans().is_empty()
            || self
                .runtime_spans()
                .iter()
                .any(|span| matches!(span.kind, SpanKind::Stack | SpanKind::StackUsed))
        {
            return Err("Heap replay lost its chunks or exposed unrelated runtime state".into());
        }
        if self.exact_snapshot() {
            return Err("Replayed heap was presented as an exact whole-memory snapshot".into());
        }
        if let Some(time) = unknown_time {
            self.select_replay_cursor(ReplayCursor::Time(time));
            if !self.runtime_spans().is_empty() {
                return Err("Mutation window exposed committed heap geometry".into());
            }
        }
        self.select_replay_cursor(ReplayCursor::Point(target));
        Ok(())
    }
}

fn evidence_label(evidence: HeapEvidence) -> &'static str {
    match evidence {
        HeapEvidence::ObservedCheckpoint => "OBSERVED HEAP CHECKPOINT",
        HeapEvidence::Reconstructed => "RECONSTRUCTED HEAP",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn app() -> MemoryExplorer {
        let mut model = nxrs_memory_explorer::demo();
        // These snapshots deliberately have the same timestamp as a journal
        // point but no shared recording identity. They must stay independent.
        for snapshot in &mut model.snapshots {
            snapshot.correlation = None;
        }
        let start = 0x2000_0800u64;
        let free = json!({"start":start,"size":256,"state":"free"});
        let split = json!([
            {"start":start,"size":64,"state":"allocated"},
            {"start":start+64,"size":192,"state":"free"}
        ]);
        let journal = json!({
            "schema_version":1,
            "build":model.build,
            "provenance":{"nuttx_revision":"a".repeat(40),"config_sha256":"b".repeat(64),"collector":"synthetic-ui-contract-fixture"},
            "recording_id":"ui-replay-test", "clock":"test-clock",
            "heap":{"id":"test-heap","epoch":"first","region":"demo-ram","start":start,"size":256},
            "initial":{"sequence":0,"time_ns":0,"chunks":[free.clone()]},
            "records":[
                {"kind":"transaction","sequence":1,"heap_epoch":"first","start_ns":2,"commit_ns":4,"start":start,"size":256,"expected":[free.clone()],"replacement":split,"label":"split"},
                {"kind":"loss","heap_epoch":"first","time_ns":8,"reason":"deliberately dropped records"},
                {"kind":"checkpoint","heap_epoch":"first","sequence":3,"time_ns":10,"chunks":[free],"label":"full recovery"}
            ],
            "closing":{"heap_epoch":"first","sequence":3,"time_ns":12}
        });
        let replay =
            nxrs_memory_explorer::parse_heap_journal(&journal.to_string(), &model, None).unwrap();
        let mut app = MemoryExplorer::new(InputPaths::default(), false, None).unwrap();
        app.install_model(model, true);
        app.replay = Some(replay);
        app.replay_enabled = true;
        let region = app
            .regions()
            .into_iter()
            .find(|r| r.key == "demo-ram")
            .unwrap();
        app.select_region(&region);
        app.select_replay_cursor(ReplayCursor::Point(0));
        app
    }

    #[test]
    fn replay_does_not_expose_coincident_snapshots_or_task_stacks() {
        let app = app();
        assert!(app.model.as_ref().unwrap().snapshot_at(0).is_some());
        assert!(!app.exact_snapshot());
        assert!(app.runtime_lane_known(2));
        assert!(!app.runtime_lane_known(3));
        assert!(app
            .runtime_spans()
            .iter()
            .all(|span| span.id.starts_with("replay/")));
        let runtime = app.runtime_spans();
        let projected = app
            .model
            .as_ref()
            .unwrap()
            .physical_intervals_with_runtime(&runtime);
        assert!(projected
            .iter()
            .all(|item| !matches!(item.span.kind, SpanKind::Stack | SpanKind::StackUsed)));
        assert!(projected
            .iter()
            .any(|item| item.span.kind == SpanKind::Free));
    }

    #[test]
    fn mutation_loss_and_outside_coverage_hide_heap_until_full_recovery() {
        let mut app = app();
        for time in [2, 3, 6, 9, 13] {
            app.select_replay_cursor(ReplayCursor::Time(time));
            assert!(
                app.runtime_spans().is_empty(),
                "unexpected heap geometry at {time}"
            );
            assert!(!app.runtime_lane_known(2));
            assert!(app.spans().iter().all(|span| !span.runtime));
        }
        app.select_replay_cursor(ReplayCursor::Time(10));
        assert!(!app.runtime_spans().is_empty());
        assert!(!app.exact_snapshot());
        assert!(!app.runtime_lane_known(3));
    }

    #[test]
    fn sequence_navigation_preserves_points_and_mode_switches_are_independent() {
        let mut app = app();
        app.adjacent_snapshot(true);
        assert_eq!(app.replay_cursor, Some(ReplayCursor::Point(1)));
        assert_eq!(app.time_ns, Some(4));
        assert!(app
            .runtime_spans()
            .iter()
            .any(|span| span.kind == SpanKind::Allocation));
        app.adjacent_snapshot(true);
        assert_eq!(app.replay_cursor, Some(ReplayCursor::Point(2)));
        app.adjacent_snapshot(false);
        assert_eq!(app.replay_cursor, Some(ReplayCursor::Point(1)));
        app.replay_enabled = false;
        app.time_ns = Some(0);
        assert!(app.exact_snapshot());
        assert!(app
            .runtime_spans()
            .iter()
            .any(|span| span.kind == SpanKind::Stack));
        app.replay_enabled = true;
        assert!(!app
            .runtime_spans()
            .iter()
            .any(|span| span.kind == SpanKind::Stack));
    }

    #[test]
    fn replay_projection_replaces_reservation_without_double_counting_aliases() {
        let app = app();
        let model = app.model.as_ref().unwrap();
        let runtime = app.runtime_spans();
        let totals = model.physical_occupied_bytes_with_runtime(&runtime);
        let static_totals = model.physical_occupied_bytes_with_runtime(&[]);
        assert!(totals["sram"] <= static_totals["sram"]);
        let projected = model.physical_occupied_intervals_with_runtime(&runtime);
        for interval in projected
            .iter()
            .filter(|interval| interval.resource == "sram")
        {
            assert!(interval.range.end <= 8192);
        }
    }

    #[test]
    fn uncovered_heap_point_clears_timing_cursor_and_cannot_open_a_stale_link() {
        let mut app = app();
        let trace = json!({
            "otherData":{"nxrs":{"schema_version":1,"recording_id":"ui-replay-test","clock":"test-clock","origin_ns":0,"elf_sha256":app.model.as_ref().unwrap().build.elf_sha256}},
            "traceEvents":[
                {"ph":"i","pid":1,"name":"start","ts":0},
                {"ph":"i","pid":1,"name":"end","ts":0.005}
            ]
        });
        app.install_trace(nxrs_memory_explorer::parse_timing(&trace.to_string()).unwrap())
            .unwrap();
        app.select_replay_cursor(ReplayCursor::Point(1));
        assert_eq!(app.timing.cursor(), Some(4));
        app.select_replay_cursor(ReplayCursor::Point(2));
        assert_eq!(app.time_ns, Some(10));
        assert_eq!(app.timing.cursor(), None);
        assert!(!app.replay_time_is_covered());
        app.view = ViewMode::Memory;
        app.open_replay_timing();
        assert_eq!(app.view, ViewMode::Memory);
        assert!(app.error.is_some());
        app.select_replay_cursor(ReplayCursor::Point(1));
        app.open_replay_timing();
        assert_eq!(app.view, ViewMode::Timing);
        assert_eq!(app.timing.cursor(), Some(4));
    }
}
