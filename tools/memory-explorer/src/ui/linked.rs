//! Navigation between explicitly correlated memory observations and trace time.
//! A timing cursor between checkpoints has no observed heap, even when both
//! surrounding snapshots are available.

use super::*;

impl MemoryExplorer {
    pub(super) fn sync_timing_cursor(&mut self) {
        let time = self.correlation.as_ref().and_then(|correlation| {
            self.timing
                .cursor()
                .and_then(|cursor| correlation.source_time_ns(cursor))
        });
        if self.correlation.is_some() && self.time_ns != time {
            self.time_ns = time;
            self.selected = None;
        }
    }

    pub(super) fn open_timing_at(&mut self, trace_time_ns: u64) {
        let (Some(trace), Some(correlation)) = (&self.trace, &self.correlation) else {
            return;
        };
        if let Some(link) = correlation
            .links
            .iter()
            .find(|link| link.trace_time_ns == trace_time_ns)
        {
            self.timing.seek_checkpoint(link, trace);
        } else {
            self.timing.seek_time(trace_time_ns, trace);
        }
        self.view = ViewMode::Timing;
        self.sync_timing_cursor();
    }

    fn open_memory_at_cursor(&mut self) {
        if self.correlation.is_none() || self.timing.cursor().is_none() {
            return;
        }
        self.sync_timing_cursor();
        self.view = ViewMode::Memory;
        self.selected = None;
        // Prefer an observed allocator's address space so the cross-link opens
        // actual memory geometry, rather than an unrelated image-file window.
        let arena_region = self.model.as_ref().and_then(|model| {
            self.time_ns
                .and_then(|time| model.snapshot_at(time))
                .and_then(|snapshot| {
                    snapshot
                        .spans
                        .iter()
                        .find(|span| span.kind == SpanKind::HeapArena)
                })
                .map(|span| span.region.clone())
        });
        if let Some(arena_region) = arena_region {
            if !self
                .current_region()
                .is_some_and(|region| region.members.contains(&arena_region))
            {
                if let Some(region) = self
                    .regions()
                    .iter()
                    .find(|region| region.members.contains(&arena_region))
                {
                    self.select_region(region);
                }
            }
        }
    }

    pub(super) fn linked_checkpoints(&mut self, ctx: &egui::Context) {
        let (Some(correlation), Some(trace), Some(model)) =
            (&self.correlation, &self.trace, &self.model)
        else {
            return;
        };
        let range = timing::bounds(trace);
        let origin = trace.bounds.start_ns;
        let cursor = self.timing.cursor();
        let checkpoints: Vec<_> = correlation
            .links
            .iter()
            .map(|link| {
                (
                    link.checkpoint_id,
                    link.trace_time_ns,
                    link.snapshot_time_ns,
                    model.snapshots[link.snapshot_index].label.clone(),
                )
            })
            .collect();
        let exact = cursor.and_then(|time| checkpoints.iter().find(|point| point.1 == time));
        let mut seek = None;
        let mut open_memory = false;
        egui::TopBottomPanel::bottom(self.widget_id("linked-memory-checkpoints"))
            .exact_height(184.0)
            .frame(egui::Frame::new().fill(PANEL).inner_margin(egui::Margin::symmetric(20, 12)))
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    eyebrow(ui, "MEMORY CHECKPOINTS");
                    badge(ui, if exact.is_some() { "OBSERVED MEMORY" } else { "RUNTIME STATE UNKNOWN" }, if exact.is_some() { ACCENT } else { WARNING });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.add_enabled(cursor.is_some(), egui::Button::new(if exact.is_some() { "Open captured memory" } else { "Show memory evidence gap" })).clicked() {
                            open_memory = true;
                        }
                        let next = checkpoints.iter().find(|point| cursor.is_none_or(|time| point.1 > time));
                        if ui.add_enabled(next.is_some(), egui::Button::new("Next  ›")).clicked() {
                            seek = next.map(|point| point.1);
                        }
                        let previous = checkpoints.iter().rev().find(|point| cursor.is_some_and(|time| point.1 < time));
                        if ui.add_enabled(previous.is_some(), egui::Button::new("‹  Previous")).clicked() {
                            seek = previous.map(|point| point.1);
                        }
                    });
                });
                let (rect, response) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 69.0), Sense::click_and_drag());
                let line = rect.shrink2(Vec2::new(18.0, 0.0));
                let y = rect.top() + 22.0;
                let at = |time| line.left() + range.fraction(time) as f32 * line.width();
                let painter = ui.painter();
                let mut x = line.left();
                while x < line.right() {
                    painter.line_segment([Pos2::new(x, y), Pos2::new((x + 5.0).min(line.right()), y)], Stroke::new(1.0_f32, LINE));
                    x += 10.0;
                }
                if let Some(pointer) = response.interact_pointer_pos().filter(|_| response.clicked() || response.dragged()) {
                    seek = checkpoints.iter()
                        .min_by(|a, b| (at(a.1) - pointer.x).abs().total_cmp(&(at(b.1) - pointer.x).abs()))
                        .filter(|point| (at(point.1) - pointer.x).abs() <= 9.0)
                        .map(|point| point.1)
                        .or_else(|| Some(range.at_fraction(((pointer.x - line.left()) / line.width()) as f64)));
                }
                let stride = checkpoints.len().div_ceil((line.width() / 240.0).max(1.0) as usize);
                for (index, point) in checkpoints.iter().enumerate() {
                    let x = at(point.1);
                    let selected = cursor == Some(point.1);
                    painter.circle_filled(Pos2::new(x, y), if selected { 6.0 } else { 4.0 }, ACCENT);
                    if selected {
                        painter.circle_stroke(Pos2::new(x, y), 10.0, Stroke::new(1.0_f32, ACCENT));
                    }
                    let align = if index == 0 { Align2::LEFT_TOP } else if index + 1 == checkpoints.len() { Align2::RIGHT_TOP } else { Align2::CENTER_TOP };
                    if selected || index == 0 || index + 1 == checkpoints.len() || index % stride == 0 {
                        painter.text(Pos2::new(x, y + 15.0), align,
                            format!("{} · {}\n+{} ns", point.0, abbreviated(&point.3, 26), point.1 - origin),
                            FontId::proportional(10.0), if selected { TEXT } else { MUTED });
                    }
                }
                if let Some(cursor) = cursor {
                    let x = at(cursor);
                    painter.line_segment([Pos2::new(x, rect.top() + 2.0), Pos2::new(x, y + 10.0)], Stroke::new(1.0_f32, if exact.is_some() { ACCENT } else { WARNING }));
                }
                if let Some(point) = exact {
                    ui.label(RichText::new(format!("{} · source clock {} ns · checkpoint {}", point.3, point.2, point.0)).color(TEXT));
                } else {
                    ui.label(RichText::new("No heap observation at the cursor. Runtime blocks remain hidden between checkpoints.").color(WARNING));
                }
                ui.label(RichText::new("Full recording · points are observed memory; dashed gaps are unknown · times are relative to the first trace record.").small().color(MUTED));
            });
        if let Some(time) = seek {
            self.open_timing_at(time);
        }
        if open_memory {
            self.open_memory_at_cursor();
        }
    }

    pub(super) fn smoke_linked_navigation(&mut self) -> Result<(), String> {
        let Some(correlation) = &self.correlation else {
            return Ok(());
        };
        let first = correlation
            .links
            .first()
            .ok_or("Linked capture has no checkpoints")?
            .clone();
        let unobserved_time = correlation.links.windows(2).find_map(|pair| {
            (pair[1].trace_time_ns.saturating_sub(pair[0].trace_time_ns) > 1)
                .then(|| pair[0].trace_time_ns + 1)
        });
        self.open_timing_at(first.trace_time_ns);
        self.open_memory_at_cursor();
        if self.time_ns != Some(first.snapshot_time_ns) || !self.exact_snapshot() {
            return Err("Timing checkpoint did not open the exact captured memory".into());
        }
        if let Some(time) = unobserved_time {
            // Choose a genuinely unobserved coordinate: adjacent checkpoints
            // can be only one nanosecond apart. Do not hold the previous state.
            self.open_timing_at(time);
            self.open_memory_at_cursor();
            if self.exact_snapshot() || self.spans().iter().any(|span| span.runtime) {
                return Err("Linked timing gap exposed uncaptured runtime memory".into());
            }
        }
        self.open_timing_at(first.trace_time_ns);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use nxrs_memory_explorer::SnapshotCorrelation;
    use serde_json::json;

    fn linked_app() -> MemoryExplorer {
        linked_app_at(&[1000, 2000, 3000])
    }

    fn linked_app_at(offsets: &[u64]) -> MemoryExplorer {
        let mut model = nxrs_memory_explorer::demo();
        let origin = (1_u64 << 53) + 1;
        model.snapshots.truncate(offsets.len());
        for (index, snapshot) in model.snapshots.iter_mut().enumerate() {
            snapshot.time_ns = origin + offsets[index];
            snapshot.correlation = Some(SnapshotCorrelation {
                recording_id: "synthetic-test-recording".into(),
                clock: "synthetic-test-clock".into(),
                checkpoint_id: index as u64 + 1,
            });
        }
        let trace = json!({
            "otherData": {"nxrs": {
                "schema_version": 1,
                "recording_id": "synthetic-test-recording",
                "clock": "synthetic-test-clock",
                "origin_ns": origin,
                "elf_sha256": model.build.elf_sha256,
            }},
            "traceEvents": model.snapshots.iter().enumerate().map(|(index, _)| json!({
                "name": format!("memory/checkpoint/{}", index + 1),
                "ph": "i", "pid": 1,
                "ts": format!("{}.{:03}", offsets[index] / 1000, offsets[index] % 1000).parse::<serde_json::Number>().unwrap(),
            })).collect::<Vec<_>>(),
        });
        let mut app = MemoryExplorer::new(InputPaths::default(), false, None).unwrap();
        app.install_model(model, true);
        app.install_trace(nxrs_memory_explorer::parse_timing(&trace.to_string()).unwrap())
            .unwrap();
        app
    }

    #[test]
    fn timing_checkpoint_and_memory_cross_links_retain_exact_source_nanoseconds() {
        let mut app = linked_app();
        let point = app.correlation.as_ref().unwrap().links[1].clone();
        app.open_timing_at(point.trace_time_ns);
        assert_eq!(app.timing.cursor(), Some(point.trace_time_ns));
        app.open_memory_at_cursor();
        assert_eq!(app.view, ViewMode::Memory);
        assert_eq!(app.time_ns, Some(point.snapshot_time_ns));
        assert!(app.exact_snapshot());
        assert!(app.spans().iter().any(|span| span.runtime));
        app.open_timing_at(point.trace_time_ns);
        assert_eq!(app.view, ViewMode::Timing);
        assert_eq!(app.time_ns, Some(point.snapshot_time_ns));
    }

    #[test]
    fn timing_cursor_in_gap_hides_runtime_geometry_and_preserves_static_sections() {
        let mut app = linked_app();
        let point = app.correlation.as_ref().unwrap().links[1].clone();
        app.open_timing_at(point.trace_time_ns + 1);
        app.open_memory_at_cursor();
        assert_eq!(app.time_ns, Some(point.snapshot_time_ns + 1));
        assert!(!app.exact_snapshot());
        let spans = app.spans();
        assert!(!spans.iter().any(|span| span.runtime));
        assert!(spans.iter().any(|span| span.span.kind == SpanKind::Bss));
    }

    #[test]
    fn independent_trace_cursor_never_changes_legacy_snapshot_selection() {
        let mut app = MemoryExplorer::new(InputPaths::default(), true, None).unwrap();
        let before = app.time_ns;
        let trace = app.trace.as_ref().unwrap();
        app.timing.seek_time(trace.bounds.end_ns, trace);
        app.sync_timing_cursor();
        assert_eq!(app.time_ns, before);
        assert!(app.correlation.is_none());
    }

    #[test]
    fn incompatible_recording_cannot_replace_an_installed_link() {
        let mut app = linked_app();
        let before = app.correlation.clone();
        let mut trace = app.trace.clone().unwrap();
        trace.metadata.recording_id = Some("another-recording".into());
        assert!(app.install_trace(trace).is_err());
        assert_eq!(app.correlation, before);
        assert_eq!(
            app.trace.as_ref().unwrap().metadata.recording_id.as_deref(),
            Some("synthetic-test-recording")
        );
    }

    #[test]
    fn linked_render_smoke_checks_observed_and_unknown_paths() {
        let mut app = linked_app();
        app.smoke_linked_navigation().unwrap();
        assert_eq!(app.view, ViewMode::Timing);
        assert!(app.exact_snapshot());
    }

    #[test]
    fn linked_smoke_accepts_adjacent_nanosecond_checkpoints() {
        for times in [
            &[1000, 1001, 2000][..],
            &[1000, 1001, 1002][..],
            &[1000][..],
        ] {
            let mut app = linked_app_at(times);
            app.smoke_linked_navigation().unwrap();
            assert!(app.exact_snapshot());
        }
    }
}
