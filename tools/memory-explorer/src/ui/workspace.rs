//! Recording ownership lives above the views. A tab keeps its already validated
//! evidence and all navigation state; switching never reloads or combines files.

use super::*;
use std::io::Read;

pub(super) enum OpenRequest {
    New,
    Inputs(InputPaths),
    Session(PathBuf),
    Demo,
}

struct RecordingTab {
    id: u64,
    label: String,
    source: String,
    viewer: MemoryExplorer,
}

pub struct RecordingWorkspace {
    tabs: Vec<RecordingTab>,
    active: usize,
    next_id: u64,
    notice: Option<String>,
    reveal_active: bool,
}

impl RecordingWorkspace {
    pub fn new(
        inputs: InputPaths,
        demo: bool,
        sessions: Vec<PathBuf>,
        smoke: Option<SmokeOptions>,
    ) -> Result<Self, String> {
        let first = MemoryExplorer::new(inputs, demo, smoke)?;
        let mut workspace = Self {
            tabs: vec![make_tab(1, first, None)],
            active: 0,
            next_id: 2,
            notice: None,
            reveal_active: true,
        };
        for session in sessions {
            workspace.open(OpenRequest::Session(session))?;
        }
        workspace.active = 0;
        Ok(workspace)
    }

    pub fn set_view(&mut self, view: ViewMode) -> Result<(), String> {
        self.tabs[self.active].viewer.set_view(view)
    }

    pub fn set_timing_analysis(&mut self, mode: &str) {
        self.tabs[self.active].viewer.set_timing_analysis(mode);
    }

    fn open(&mut self, request: OpenRequest) -> Result<(), String> {
        let (mut incoming, session) = match request {
            OpenRequest::New => {
                self.new_tab();
                return Ok(());
            }
            OpenRequest::Session(path) => {
                let inputs = InputPaths::from_session(&path)?;
                (MemoryExplorer::new(inputs, false, None)?, Some(path))
            }
            OpenRequest::Inputs(inputs) => {
                if !inputs.has_inputs() {
                    return Err("Choose a session, final ELF, or timing trace to open.".into());
                }
                (MemoryExplorer::new(inputs, false, None)?, None)
            }
            OpenRequest::Demo => (
                MemoryExplorer::new(InputPaths::default(), true, None)?,
                None,
            ),
        };
        // Validation above completes before any currently loaded recording is
        // touched. An empty input tab may be filled; loaded evidence is retained.
        let current = &mut self.tabs[self.active];
        if current.viewer.is_empty() {
            incoming.smoke = current.viewer.smoke.take();
            *current = make_tab(current.id, incoming, session.as_deref());
        } else {
            current.viewer.error = None;
            let id = self.next_id;
            self.next_id += 1;
            self.tabs.push(make_tab(id, incoming, session.as_deref()));
            self.active = self.tabs.len() - 1;
        }
        self.notice = None;
        self.reveal_active = true;
        Ok(())
    }

    fn new_tab(&mut self) {
        let id = self.next_id;
        self.next_id += 1;
        self.tabs.push(make_tab(
            id,
            MemoryExplorer::new(InputPaths::default(), false, None)
                .expect("An empty recording has no inputs to reject"),
            None,
        ));
        self.active = self.tabs.len() - 1;
        self.reveal_active = true;
    }

    fn select(&mut self, index: usize) {
        if index < self.tabs.len() {
            self.active = index;
            self.reveal_active = true;
        }
    }

    fn cycle(&mut self, backwards: bool) {
        self.select(if backwards {
            (self.active + self.tabs.len() - 1) % self.tabs.len()
        } else {
            (self.active + 1) % self.tabs.len()
        });
    }

    fn close(&mut self, index: usize) {
        if index >= self.tabs.len() {
            return;
        }
        self.tabs.remove(index);
        if self.tabs.is_empty() {
            self.new_tab();
        } else {
            if index < self.active {
                self.active -= 1;
            } else if index == self.active {
                self.active = index.min(self.tabs.len() - 1);
            }
            self.reveal_active = true;
        }
    }

    fn keyboard(&mut self, ctx: &egui::Context) {
        // These are application shortcuts, also available while a path/filter
        // field is focused. Unmodified editing/navigation keys stay in the view.
        let action = ctx.input_mut(|input| {
            if input.consume_shortcut(&egui::KeyboardShortcut::new(
                egui::Modifiers::COMMAND,
                egui::Key::T,
            )) {
                Some(0)
            } else if input.consume_shortcut(&egui::KeyboardShortcut::new(
                egui::Modifiers::COMMAND,
                egui::Key::W,
            )) {
                Some(1)
            } else if input.consume_shortcut(&egui::KeyboardShortcut::new(
                egui::Modifiers::CTRL | egui::Modifiers::SHIFT,
                egui::Key::Tab,
            )) {
                Some(2)
            } else if input.consume_shortcut(&egui::KeyboardShortcut::new(
                egui::Modifiers::CTRL,
                egui::Key::Tab,
            )) {
                Some(3)
            } else {
                None
            }
        });
        match action {
            Some(0) => self.new_tab(),
            Some(1) => self.close(self.active),
            Some(2) => self.cycle(true),
            Some(3) => self.cycle(false),
            _ => return,
        }
        clear_focus(ctx);
    }

    fn tab_bar(&mut self, ctx: &egui::Context) {
        let mut select = None;
        let mut close = None;
        let mut add = false;
        egui::TopBottomPanel::top("recording-tabs")
            .frame(
                egui::Frame::new()
                    .fill(BACKGROUND)
                    .inner_margin(egui::Margin::symmetric(12, 7)),
            )
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.label(RichText::new("RECORDINGS").small().color(MUTED));
                    add = ui
                        .button("+ New")
                        .on_hover_text("Open another recording · Ctrl/Cmd+T")
                        .clicked();
                    egui::ScrollArea::horizontal()
                        .id_salt("recording-tab-scroll")
                        .auto_shrink([false, true])
                        .show(ui, |ui| {
                            ui.horizontal(|ui| {
                                for (index, tab) in self.tabs.iter().enumerate() {
                                    ui.push_id(tab.id, |ui| {
                                        let response = ui
                                            .selectable_label(
                                                index == self.active,
                                                format!(
                                                    "{} · {}",
                                                    tab.id,
                                                    abbreviated(&tab.label, 30)
                                                ),
                                            )
                                            .on_hover_text(&tab.source);
                                        if response.clicked() {
                                            select = Some(index);
                                        }
                                        if response.clicked_by(egui::PointerButton::Middle) {
                                            close = Some(index);
                                        }
                                        if self.reveal_active && index == self.active {
                                            response.scroll_to_me(Some(egui::Align::Center));
                                        }
                                        if ui
                                            .small_button("×")
                                            .on_hover_text(format!(
                                                "Close {} · Ctrl/Cmd+W",
                                                tab.label
                                            ))
                                            .clicked()
                                        {
                                            close = Some(index);
                                        }
                                        ui.separator();
                                    });
                                }
                            });
                        });
                });
                if let Some(message) = &self.notice {
                    let mut dismiss = false;
                    ui.horizontal_wrapped(|ui| {
                        ui.label(RichText::new(message).color(Color32::LIGHT_RED));
                        dismiss = ui.small_button("Dismiss").clicked();
                    });
                    if dismiss {
                        self.notice = None;
                    }
                }
            });
        self.reveal_active = false;
        if let Some(index) = select {
            self.select(index);
            clear_focus(ctx);
        }
        if let Some(index) = close {
            self.close(index);
            clear_focus(ctx);
        }
        if add {
            self.new_tab();
            clear_focus(ctx);
        }
    }

    fn dropped_files(&mut self, ctx: &egui::Context) {
        let paths: Vec<_> = ctx
            .input_mut(|input| std::mem::take(&mut input.raw.dropped_files))
            .into_iter()
            .filter_map(|file| file.path)
            .collect();
        if paths.is_empty() {
            return;
        }
        self.open_dropped(paths);
    }

    fn open_dropped(&mut self, paths: Vec<PathBuf>) {
        let mut raw = Vec::new();
        let mut failures = Vec::new();
        for path in paths {
            match classify(&path) {
                Ok(DroppedKind::Session) => {
                    if let Err(error) = self.open(OpenRequest::Session(path.clone())) {
                        failures.push(format!("{}: {error}", path.display()));
                    }
                }
                Ok(kind) => raw.push((path, kind)),
                Err(error) => failures.push(error),
            }
        }
        let drafting = self.tabs[self.active].viewer.is_empty()
            && self.tabs[self.active].viewer.inputs.has_inputs();
        if raw.iter().all(|(_, kind)| *kind == DroppedKind::Trace) && !(drafting && raw.len() == 1)
        {
            for (path, _) in raw {
                let inputs = InputPaths {
                    trace: path.to_string_lossy().into_owned(),
                    ..Default::default()
                };
                if let Err(error) = self.open(OpenRequest::Inputs(inputs)) {
                    failures.push(format!("{}: {error}", path.display()));
                }
            }
        } else if !raw.is_empty() {
            // A group of raw artifacts needs one unambiguous owner. Never attach
            // it to the active loaded recording, nor guess across several ELFs.
            let mut inputs = if self.tabs[self.active].viewer.is_empty() {
                self.tabs[self.active].viewer.inputs.clone()
            } else {
                InputPaths::default()
            };
            inputs.session_verified = false;
            let mut conflict = false;
            for (path, kind) in raw {
                let value = path.to_string_lossy().into_owned();
                let slot = match kind {
                    DroppedKind::Elf => Some(&mut inputs.elf),
                    DroppedKind::Map => Some(&mut inputs.map),
                    DroppedKind::Layout => Some(&mut inputs.layout),
                    DroppedKind::Trace => Some(&mut inputs.trace),
                    DroppedKind::Journal => Some(&mut inputs.heap_journal),
                    DroppedKind::Snapshot => {
                        if !inputs.snapshots.contains(&value) {
                            inputs.snapshots.push(value.clone());
                        }
                        None
                    }
                    DroppedKind::Session => unreachable!(),
                };
                if let Some(slot) = slot {
                    if slot.is_empty() || *slot == value {
                        *slot = value;
                    } else {
                        conflict = true;
                    }
                }
            }
            if conflict {
                failures.push("This drop contains conflicting artifacts or conflicts with the current draft. Use + New for another recording. The current draft was kept; no ambiguous artifacts were combined.".into());
            } else {
                if !self.tabs[self.active].viewer.is_empty() {
                    self.new_tab();
                }
                self.tabs[self.active].viewer.inputs = inputs;
                self.tabs[self.active].viewer.error = None;
                self.tabs[self.active].label = "New recording".into();
                self.tabs[self.active].source =
                    "Review the dropped artifacts, then open evidence in this tab.".into();
            }
        }
        self.notice = (!failures.is_empty()).then(|| failures.join("\n"));
    }

    fn show(&mut self, ctx: &egui::Context) {
        self.dropped_files(ctx);
        self.keyboard(ctx);
        self.tab_bar(ctx);
        self.tabs[self.active].viewer.show(ctx);
        if let Some(request) = self.tabs[self.active].viewer.pending_open.take() {
            match self.open(request) {
                Ok(()) => clear_focus(ctx),
                Err(error) => self.tabs[self.active].viewer.error = Some(error),
            }
            ctx.request_repaint();
        }
    }
}

impl eframe::App for RecordingWorkspace {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.show(ctx);
    }
}

fn clear_focus(ctx: &egui::Context) {
    ctx.memory_mut(|memory| {
        if let Some(id) = memory.focused() {
            memory.surrender_focus(id);
        }
    });
}

fn make_tab(id: u64, mut viewer: MemoryExplorer, session: Option<&Path>) -> RecordingTab {
    viewer.namespace = egui::Id::new(("recording", id));
    let (label, origin) = if let Some(path) = session {
        let named = if path.file_name().is_some_and(|name| name == "session.json") {
            path.parent().unwrap_or(path)
        } else {
            path
        };
        let label = if viewer.inputs.recording_label.trim().is_empty() {
            path_label(named)
        } else {
            viewer.inputs.recording_label.clone()
        };
        (label, format!("Session: {}", path.display()))
    } else if viewer.synthetic {
        (
            "Synthetic demonstration".into(),
            "Synthetic example · no measured device evidence".into(),
        )
    } else if !viewer.inputs.trace.is_empty() {
        (
            path_label(Path::new(&viewer.inputs.trace)),
            format!("Trace: {}", viewer.inputs.trace),
        )
    } else if !viewer.inputs.elf.is_empty() {
        (
            path_label(Path::new(&viewer.inputs.elf)),
            format!("ELF: {}", viewer.inputs.elf),
        )
    } else {
        (
            "New recording".into(),
            "Choose a capture session, firmware artifacts, or a standalone trace.".into(),
        )
    };
    let mut source = if let Some(model) = &viewer.model {
        format!(
            "{origin}\nBuild: {}\nELF SHA-256: {}",
            model.build.label, model.build.elf_sha256
        )
    } else {
        origin
    };
    if let Some(recording) = viewer
        .trace
        .as_ref()
        .and_then(|trace| trace.metadata.recording_id.as_ref())
    {
        source.push_str(&format!("\nRecording: {recording}"));
    }
    RecordingTab {
        id,
        label,
        source,
        viewer,
    }
}

fn path_label(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| path.display().to_string())
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DroppedKind {
    Session,
    Elf,
    Map,
    Layout,
    Trace,
    Journal,
    Snapshot,
}

fn classify(path: &Path) -> Result<DroppedKind, String> {
    if path.is_dir() {
        return Ok(DroppedKind::Session);
    }
    let extension = path
        .extension()
        .and_then(|extension| extension.to_str())
        .map(str::to_ascii_lowercase);
    match extension.as_deref() {
        Some("map") => Ok(DroppedKind::Map),
        Some("json") => {
            let metadata =
                std::fs::metadata(path).map_err(|error| format!("{}: {error}", path.display()))?;
            if !metadata.is_file() {
                return Err(format!("{} is not a regular file", path.display()));
            }
            let size = metadata.len();
            if size > 128 * 1024 * 1024 {
                return Err(format!(
                    "{} exceeds the 128 MiB input limit",
                    path.display()
                ));
            }
            let file = std::fs::File::open(path)
                .map_err(|error| format!("{}: {error}", path.display()))?;
            let mut bytes = Vec::new();
            file.take(128 * 1024 * 1024 + 1)
                .read_to_end(&mut bytes)
                .map_err(|error| format!("{}: {error}", path.display()))?;
            if bytes.len() > 128 * 1024 * 1024 {
                return Err(format!(
                    "{} exceeds the 128 MiB input limit",
                    path.display()
                ));
            }
            let json: serde_json::Value = serde_json::from_slice(&bytes)
                .map_err(|error| format!("{}: {error}", path.display()))?;
            Ok(
                if json.get("files").is_some() && json.get("elf").is_some() {
                    DroppedKind::Session
                } else if json.is_array() || json.get("traceEvents").is_some() {
                    DroppedKind::Trace
                } else if json.get("heap").is_some() && json.get("records").is_some() {
                    DroppedKind::Journal
                } else if json.get("regions").is_some() && json.get("snapshots").is_none() {
                    DroppedKind::Layout
                } else {
                    DroppedKind::Snapshot
                },
            )
        }
        _ => Ok(DroppedKind::Elf),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(0);

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "nxrs-recording-tabs-{}-{}",
                std::process::id(),
                NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        fn trace(&self, recording: &str, name: &str) -> PathBuf {
            let directory = self.0.join(recording);
            std::fs::create_dir_all(&directory).unwrap();
            let path = directory.join("trace.json");
            std::fs::write(&path, serde_json::to_vec(&serde_json::json!({
                "traceEvents": [{"ph":"X","pid":1,"tid":1,"ts":1,"dur":2,"name":name,"cat":"app"}],
                "otherData":{"nxrs":{"schema_version":1,"recording_id":recording,"clock":"fixture-clock"}}
            })).unwrap()).unwrap();
            path
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn empty() -> RecordingWorkspace {
        RecordingWorkspace::new(InputPaths::default(), false, Vec::new(), None).unwrap()
    }

    #[test]
    fn independent_traces_remain_loaded_when_switching_after_files_are_removed() {
        let fixture = Fixture::new();
        let first = fixture.trace("recording-a", "imu/read");
        let second = fixture.trace("recording-b", "gnss/read");
        let mut workspace = empty();
        workspace.open_dropped(vec![first.clone(), second.clone()]);
        assert_eq!(workspace.tabs.len(), 2);
        assert_eq!(workspace.tabs[0].label, workspace.tabs[1].label);
        assert_ne!(workspace.tabs[0].id, workspace.tabs[1].id);
        assert!(workspace.tabs[0].source.contains("recording-a"));
        assert!(workspace.tabs[1].source.contains("recording-b"));
        {
            let first = &mut workspace.tabs[0].viewer;
            first.timing.seek_time(1_500, first.trace.as_ref().unwrap());
        }
        {
            let second = &mut workspace.tabs[1].viewer;
            second
                .timing
                .seek_time(2_500, second.trace.as_ref().unwrap());
        }
        std::fs::remove_file(first).unwrap();
        std::fs::remove_file(second).unwrap();
        workspace.select(0);
        assert_eq!(
            workspace.tabs[workspace.active].viewer.timing.cursor(),
            Some(1_500)
        );
        assert_eq!(
            workspace.tabs[workspace.active]
                .viewer
                .trace
                .as_ref()
                .unwrap()
                .events[0]
                .name,
            "imu/read"
        );
        workspace.cycle(false);
        assert_eq!(
            workspace.tabs[workspace.active].viewer.timing.cursor(),
            Some(2_500)
        );
        assert_eq!(
            workspace.tabs[workspace.active]
                .viewer
                .trace
                .as_ref()
                .unwrap()
                .events[0]
                .name,
            "gnss/read"
        );
    }

    #[test]
    fn tab_switches_preserve_memory_selection_zoom_filter_and_comparison() {
        let mut workspace = empty();
        workspace.open(OpenRequest::Demo).unwrap();
        let (view, selected, observed, comparison, retained_spans) = {
            let first = &mut workspace.tabs[0].viewer;
            first.set_view(ViewMode::Memory).unwrap();
            first.switch_address_mode(true);
            let heap_region = first
                .regions()
                .into_iter()
                .find(|region| region.key == "sram")
                .unwrap();
            first.select_region(&heap_region);
            let span = first
                .spans()
                .into_iter()
                .find(|span| span.span.kind == SpanKind::Allocation)
                .unwrap();
            first.focus(&span);
            first.filter = "imu".into();
            first.time_ns = Some(500_000_000);
            first.comparison = Some(0);
            (
                first.viewport,
                first.selected.clone(),
                first.time_ns,
                first.comparison,
                first.model.as_ref().unwrap().static_spans.as_ptr(),
            )
        };
        workspace.open(OpenRequest::Demo).unwrap();
        let second = &mut workspace.tabs[1].viewer;
        second.filter = "gnss".into();
        second.switch_address_mode(false);
        workspace.select(0);
        let first = &workspace.tabs[workspace.active].viewer;
        assert_eq!(first.viewport, view);
        assert_eq!(first.selected, selected);
        assert_eq!(first.time_ns, observed);
        assert_eq!(first.comparison, comparison);
        assert_eq!(first.filter, "imu");
        assert!(first.physical);
        assert_eq!(first.view, ViewMode::Memory);
        assert_eq!(
            first.model.as_ref().unwrap().static_spans.as_ptr(),
            retained_spans
        );
        assert_ne!(
            first.widget_id("regions"),
            workspace.tabs[1].viewer.widget_id("regions")
        );
    }

    #[test]
    fn failed_open_and_ambiguous_drop_leave_loaded_recordings_intact() {
        let mut workspace = empty();
        workspace.open(OpenRequest::Demo).unwrap();
        let id = workspace.tabs[0].id;
        let fixture = Fixture::new();
        let bad = fixture.0.join("trace.json");
        std::fs::write(&bad, "not a trace").unwrap();
        assert!(workspace
            .open(OpenRequest::Inputs(InputPaths {
                trace: bad.to_string_lossy().into_owned(),
                ..Default::default()
            }))
            .is_err());
        workspace.open_dropped(vec![
            fixture.0.join("first.elf"),
            fixture.0.join("second.elf"),
        ]);
        assert_eq!(workspace.tabs.len(), 1);
        assert_eq!(workspace.tabs[workspace.active].id, id);
        assert!(workspace.tabs[0].viewer.synthetic);
        assert!(workspace
            .notice
            .as_ref()
            .unwrap()
            .contains("no ambiguous artifacts"));
    }

    #[test]
    fn closing_tabs_keeps_the_active_recording_and_last_close_returns_to_open_form() {
        let mut workspace = empty();
        for _ in 0..3 {
            workspace.open(OpenRequest::Demo).unwrap();
        }
        let second = workspace.tabs[1].id;
        workspace.select(1);
        workspace.close(0);
        assert_eq!(workspace.active, 0);
        assert_eq!(workspace.tabs[workspace.active].id, second);
        workspace.close(0);
        assert_eq!(workspace.tabs.len(), 1);
        let previous_namespace = workspace.tabs[0].viewer.namespace;
        workspace.close(0);
        assert_eq!(workspace.tabs.len(), 1);
        assert_eq!(workspace.active, 0);
        assert!(workspace.tabs[0].viewer.is_empty());
        assert_ne!(workspace.tabs[0].viewer.namespace, previous_namespace);
    }

    #[test]
    fn initial_view_applies_only_to_the_active_recording_in_a_mixed_workspace() {
        let fixture = Fixture::new();
        let trace_path = fixture.trace("timing-recording", "imu/read");
        let mut workspace = empty();
        workspace
            .open(OpenRequest::Inputs(InputPaths {
                trace: trace_path.to_string_lossy().into_owned(),
                ..Default::default()
            }))
            .unwrap();
        // A capture with memory snapshots and no execution trace has a valid
        // overview; a command-line timing preference for tab 1 cannot reject it.
        let mut memory_only = MemoryExplorer::new(InputPaths::default(), false, None).unwrap();
        memory_only.install_model(nxrs_memory_explorer::demo(), true);
        assert!(memory_only.trace.is_none());
        assert!(!memory_only.model.as_ref().unwrap().snapshots.is_empty());
        workspace.tabs.push(make_tab(2, memory_only, None));
        workspace.next_id = 3;
        workspace.set_view(ViewMode::Timing).unwrap();
        workspace.set_timing_analysis("events");
        assert_eq!(workspace.tabs[0].viewer.view, ViewMode::Timing);
        assert_eq!(workspace.tabs[1].viewer.view, ViewMode::Overview);
        workspace.select(1);
        workspace.set_view(ViewMode::Memory).unwrap();
        assert_eq!(workspace.tabs[0].viewer.view, ViewMode::Timing);
        assert_eq!(workspace.tabs[1].viewer.view, ViewMode::Memory);
    }

    #[test]
    fn sequential_raw_drops_complete_only_the_empty_draft_and_reject_conflicts() {
        let fixture = Fixture::new();
        let elf = fixture.0.join("nuttx.elf");
        let map = fixture.0.join("nuttx.map");
        let snapshot = fixture.0.join("snapshot.json");
        std::fs::write(&snapshot, r#"{"snapshots":[]}"#).unwrap();
        let trace = fixture.trace("recording-a", "imu/read");
        let mut workspace = empty();
        workspace.open_dropped(vec![elf.clone()]);
        workspace.open_dropped(vec![map.clone()]);
        workspace.open_dropped(vec![snapshot.clone()]);
        workspace.open_dropped(vec![trace.clone()]);
        assert_eq!(workspace.tabs.len(), 1);
        let inputs = &workspace.tabs[0].viewer.inputs;
        assert_eq!(inputs.elf, elf.to_string_lossy());
        assert_eq!(inputs.map, map.to_string_lossy());
        assert_eq!(
            inputs.snapshots,
            vec![snapshot.to_string_lossy().into_owned()]
        );
        assert_eq!(inputs.trace, trace.to_string_lossy());
        assert!(workspace.tabs[0].viewer.is_empty());
        workspace.open_dropped(vec![fixture.0.join("another.elf")]);
        assert_eq!(workspace.tabs.len(), 1);
        assert_eq!(workspace.tabs[0].viewer.inputs.elf, elf.to_string_lossy());
        assert_eq!(workspace.tabs[0].viewer.inputs.map, map.to_string_lossy());
        assert!(workspace
            .notice
            .as_ref()
            .unwrap()
            .contains("current draft was kept"));
        workspace.open(OpenRequest::Demo).unwrap();
        workspace.open_dropped(vec![map]);
        assert_eq!(workspace.tabs.len(), 2);
        assert!(workspace.tabs[0].viewer.synthetic);
        assert!(workspace.tabs[1].viewer.inputs.elf.is_empty());
    }

    fn frame(
        workspace: &mut RecordingWorkspace,
        ctx: &egui::Context,
        events: Vec<egui::Event>,
    ) -> egui::FullOutput {
        ctx.run(
            egui::RawInput {
                screen_rect: Some(Rect::from_min_size(Pos2::ZERO, Vec2::new(1440.0, 1050.0))),
                events,
                ..Default::default()
            },
            |ctx| workspace.show(ctx),
        )
    }

    fn text_position(output: &egui::FullOutput, text: &str) -> Pos2 {
        output
            .shapes
            .iter()
            .find_map(|clipped| {
                if let egui::epaint::Shape::Text(shape) = &clipped.shape {
                    if shape.galley.text() == text {
                        return Some(shape.pos + shape.galley.size() / 2.0);
                    }
                }
                None
            })
            .unwrap_or_else(|| {
                panic!(
                    "Missing visible label: {text}; rendered text: {:?}",
                    output
                        .shapes
                        .iter()
                        .filter_map(|clipped| match &clipped.shape {
                            egui::epaint::Shape::Text(shape) => Some(shape.galley.text()),
                            _ => None,
                        })
                        .collect::<Vec<_>>()
                )
            })
    }

    fn click(workspace: &mut RecordingWorkspace, ctx: &egui::Context, at: Pos2) {
        for pressed in [true, false] {
            frame(
                workspace,
                ctx,
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
    }

    #[test]
    fn rendered_tab_controls_add_switch_and_close_real_recordings() {
        let mut workspace = empty();
        let fixture = Fixture::new();
        let trace_path = fixture.trace("recording-a", "imu/read");
        workspace
            .open(OpenRequest::Inputs(InputPaths {
                trace: trace_path.to_string_lossy().into_owned(),
                ..Default::default()
            }))
            .unwrap();
        workspace.open(OpenRequest::Demo).unwrap();
        let ctx = egui::Context::default();
        configure_style(&ctx);
        frame(&mut workspace, &ctx, vec![]);
        let output = frame(&mut workspace, &ctx, vec![]);
        let first = text_position(&output, "1 · trace.json");
        click(&mut workspace, &ctx, first);
        assert_eq!(workspace.active, 0);
        let output = frame(&mut workspace, &ctx, vec![]);
        click(
            &mut workspace,
            &ctx,
            text_position(&output, "Open recording"),
        );
        assert_eq!(workspace.tabs.len(), 3);
        assert!(workspace.tabs[workspace.active].viewer.is_empty());
        assert!(
            !workspace.tabs[workspace.active].viewer.inputs.has_inputs(),
            "A new recording must not inherit the previous trace, ELF, or snapshots"
        );
        assert_eq!(
            workspace.tabs[0].viewer.inputs.trace,
            trace_path.to_string_lossy()
        );
        frame(
            &mut workspace,
            &ctx,
            vec![egui::Event::Key {
                key: egui::Key::W,
                physical_key: None,
                pressed: true,
                repeat: false,
                modifiers: egui::Modifiers::COMMAND,
            }],
        );
        assert_eq!(workspace.tabs.len(), 2);
        assert!(workspace.tabs[workspace.active].viewer.synthetic);
        let output = frame(&mut workspace, &ctx, vec![]);
        click(&mut workspace, &ctx, text_position(&output, "+ New"));
        assert_eq!(workspace.tabs.len(), 3);
        assert!(!workspace.tabs[workspace.active].viewer.inputs.has_inputs());
    }

    #[test]
    fn egui_overview_scroll_position_is_independent_for_each_tab() {
        let mut workspace = empty();
        workspace.open(OpenRequest::Demo).unwrap();
        let label = workspace.tabs[0]
            .viewer
            .model
            .as_ref()
            .unwrap()
            .physical_resources[1]
            .name
            .clone();
        let ctx = egui::Context::default();
        configure_style(&ctx);
        frame(&mut workspace, &ctx, vec![]);
        let initial = text_position(&frame(&mut workspace, &ctx, vec![]), &label).y;
        frame(
            &mut workspace,
            &ctx,
            vec![
                egui::Event::PointerMoved(Pos2::new(800.0, 650.0)),
                egui::Event::MouseWheel {
                    unit: egui::MouseWheelUnit::Point,
                    delta: Vec2::new(0.0, -250.0),
                    modifiers: egui::Modifiers::NONE,
                },
            ],
        );
        for _ in 0..40 {
            frame(&mut workspace, &ctx, vec![]);
        }
        let scrolled = text_position(&frame(&mut workspace, &ctx, vec![]), &label).y;
        assert!(
            scrolled < initial - 100.0,
            "The actual overview must scroll for this witness: {initial} -> {scrolled}"
        );
        workspace.open(OpenRequest::Demo).unwrap();
        frame(&mut workspace, &ctx, vec![]);
        let second = text_position(&frame(&mut workspace, &ctx, vec![]), &label).y;
        assert!(
            (second - initial).abs() < 1.0,
            "A new tab inherited another recording's scroll: {initial} -> {second}"
        );
        workspace.select(0);
        frame(&mut workspace, &ctx, vec![]);
        let restored = text_position(&frame(&mut workspace, &ctx, vec![]), &label).y;
        assert!(
            (restored - scrolled).abs() < 1.0,
            "Switching tabs lost the retained scroll: {scrolled} -> {restored}"
        );
    }
}
