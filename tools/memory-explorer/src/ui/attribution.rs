use super::*;
use nxrs_memory_explorer::SourceFrame;

/// A display policy over recorded evidence, never a claim of semantic ownership.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(super) enum SourceChoice {
    Captured,
    #[default]
    Caller,
    Frame(usize),
}

impl SourceChoice {
    pub(super) fn label(self) -> String {
        match self {
            Self::Captured => "Captured source".into(),
            Self::Caller => "Caller candidate".into(),
            Self::Frame(index) => format!("Recorded frame {index}"),
        }
    }

    pub(super) fn frame(self, span: &Span) -> Option<&SourceFrame> {
        match self {
            Self::Captured => None,
            Self::Frame(index) => span.backtrace.get(index),
            Self::Caller => span.backtrace.iter().find(|frame| {
                (frame.file.is_some() || frame.function.is_some()) && !allocator_frame(frame)
            }),
        }
    }

    pub(super) fn source(self, span: &Span) -> String {
        if self == Self::Captured || span.backtrace.is_empty() {
            return span.source.clone().unwrap_or_else(|| "Unattributed".into());
        }
        self.frame(span)
            .and_then(|frame| frame.file.as_ref().or(frame.function.as_ref()))
            .cloned()
            .unwrap_or_else(|| "Unattributed".into())
    }
}

fn allocator_frame(frame: &SourceFrame) -> bool {
    let file = frame.file.as_deref().unwrap_or("").replace('\\', "/");
    let name = frame.function.as_deref().unwrap_or("");
    file.starts_with("mm_")
        || file.contains("/mm/")
        || file.contains("/mm_heap/")
        || file.contains("/mm_pool/")
        || file.ends_with("/lib_malloc.c")
        || file.ends_with("/lib_zalloc.c")
        || file.ends_with("/lib_calloc.c")
        || file.ends_with("/lib_realloc.c")
        || file.ends_with("/lib_memalign.c")
        || matches!(
            name,
            "malloc"
                | "calloc"
                | "realloc"
                | "memalign"
                | "zalloc"
                | "mm_malloc"
                | "mm_memalign"
                | "mm_zalloc"
                | "mm_realloc"
                | "__rust_alloc"
                | "__rust_alloc_zeroed"
                | "__rust_realloc"
        )
        || name.starts_with("alloc::alloc::")
        || name.starts_with("<std::sys::alloc::")
}

impl MemoryExplorer {
    pub(super) fn source_controls(&mut self, ui: &mut egui::Ui) {
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new("Color allocations by").small().color(MUTED));
            egui::ComboBox::from_id_salt("allocation-source-policy")
                .selected_text(self.source_choice.label())
                .show_ui(ui, |ui| {
                    ui.selectable_value(&mut self.source_choice, SourceChoice::Caller, "Caller candidate");
                    ui.selectable_value(&mut self.source_choice, SourceChoice::Captured, "Captured source");
                    let depth = self.model.as_ref().and_then(|model| self.time_ns.and_then(|time| model.snapshot_at(time)))
                        .into_iter().flat_map(|snapshot| &snapshot.spans)
                        .map(|span| span.backtrace.len()).max().unwrap_or(0);
                    for index in 0..depth {
                        ui.selectable_value(&mut self.source_choice, SourceChoice::Frame(index), format!("Recorded frame {index}"));
                    }
                });
            ui.label(RichText::new("?").color(MUTED)).on_hover_text(
                "Caller candidate selects the first resolved recorded frame outside recognized allocator files/functions. This is an explicit display heuristic, not proof of current ownership. Select any recorded frame in the inspector. Missing frames stay unattributed."
            );
            ui.checkbox(&mut self.collapse_empty, "Collapse empty tracks");
        });
    }

    pub(super) fn backtrace_inspector(&mut self, ui: &mut egui::Ui, span: &Span) {
        if span.backtrace.is_empty() {
            return;
        }
        ui.add_space(10.0);
        egui::CollapsingHeader::new("Recorded allocation call stack")
            .default_open(true)
            .show(ui, |ui| {
                ui.label(RichText::new("Saved PCs resolved with this ELF. Frames describe the recorded call chain; they are not current ownership or a live unwind.").small().color(MUTED));
                for (index, frame) in span.backtrace.iter().enumerate() {
                    let chosen = match self.source_choice {
                        SourceChoice::Frame(value) => value == index,
                        SourceChoice::Caller => self.source_choice.frame(span).is_some_and(|candidate| std::ptr::eq(candidate, frame)),
                        SourceChoice::Captured => index == 0,
                    };
                    let name = frame.function.as_deref().unwrap_or("Unresolved function");
                    if ui.selectable_label(chosen, format!("#{index}  {name}")).clicked() {
                        self.source_choice = SourceChoice::Frame(index);
                    }
                    let location = match (&frame.file, frame.line) {
                        (Some(file), Some(line)) => format!("{file}:{line}"),
                        (Some(file), None) => file.clone(),
                        _ => "Source location unknown".into(),
                    };
                    ui.label(RichText::new(format!("  {:#x}  {location}", frame.pc)).monospace().size(10.0).color(MUTED));
                }
            });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn caller_policy_preserves_unresolved_frames_and_skips_only_known_wrappers() {
        let mut model = nxrs_memory_explorer::demo();
        let span = model.snapshots[0]
            .spans
            .iter_mut()
            .find(|span| span.kind == SpanKind::Allocation)
            .unwrap();
        span.backtrace = vec![
            SourceFrame {
                pc: 0x100,
                function: Some("mm_malloc".into()),
                file: Some("mm_heap/mm_malloc.c".into()),
                line: Some(30),
            },
            SourceFrame {
                pc: 0x200,
                function: None,
                file: None,
                line: None,
            },
            SourceFrame {
                pc: 0x300,
                function: Some("samples::reserve".into()),
                file: Some("app/samples.rs".into()),
                line: Some(12),
            },
        ];
        assert_eq!(SourceChoice::Caller.source(span), "app/samples.rs");
        assert_eq!(SourceChoice::Frame(1).source(span), "Unattributed");
        assert_eq!(SourceChoice::Frame(9).source(span), "Unattributed");
        assert_eq!(SourceChoice::Frame(0).source(span), "mm_heap/mm_malloc.c");
        assert_eq!(span.backtrace[1].pc, 0x200);
    }
}
