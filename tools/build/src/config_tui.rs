//! Keyboard-driven native configuration.
mod presentation;

use crossterm::{
    event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers},
    execute,
    terminal::{self, EnterAlternateScreen, LeaveAlternateScreen},
};
use nxrs_build::{
    config_edit::{problem, Draft, Preview, Session},
    Result,
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, Borders, List, Paragraph, Wrap},
    Frame, Terminal,
};
use serde_json::Value;
use std::{
    collections::BTreeSet,
    io::{self, IsTerminal},
    sync::{mpsc, Arc},
    thread,
    time::Duration,
};

struct Restore;
impl Drop for Restore {
    fn drop(&mut self) {
        let _ = terminal::disable_raw_mode();
        let _ = execute!(io::stdout(), LeaveAlternateScreen, crossterm::cursor::Show);
    }
}

#[derive(Clone, Copy)]
enum Pane {
    List,
    Detail,
    Review,
}

enum Editor {
    Search,
    Groups {
        options: Vec<Vec<String>>,
        selected: usize,
    },
    Quit,
    Value {
        name: String,
        pin: bool,
    },
    Choose {
        options: Vec<String>,
        selected: usize,
        prerequisite: bool,
    },
}

struct Navigation {
    filter: String,
    group: Vec<String>,
    changed_only: bool,
    selected: String,
}

enum Work {
    Preview(u64, Draft),
    Save(u64, Preview, bool),
}

enum Resolved {
    Preview(u64, Preview),
    Save {
        revision: u64,
        preview: Preview,
        result: Result<Arc<Session>>,
        published: bool,
    },
}

struct Screen {
    session: Arc<Session>,
    sender: mpsc::Sender<Work>,
    draft: Draft,
    revision: u64,
    pending: bool,
    saving: bool,
    retry: bool,
    preview: Option<Preview>,
    baseline: Option<Value>,
    last_resolved: Option<Value>,
    filter: String,
    group: Vec<String>,
    changed_only: bool,
    technical: bool,
    selected: usize,
    pane: Pane,
    editor: Option<Editor>,
    input: String,
    message: String,
    save_error: Option<String>,
    scroll: u16,
    history: Vec<Navigation>,
}

pub fn select(kind: &str, flag: &str, choices: &[String], interactive: bool) -> Result<String> {
    if let [choice] = choices {
        return Ok(choice.clone());
    }
    if !interactive || !io::stdin().is_terminal() || !io::stderr().is_terminal() {
        return Err(format!("select {flag} ({})", choices.join(", ")));
    }
    use std::io::Write;
    eprintln!("Choose {kind}");
    for (index, choice) in choices.iter().enumerate() {
        eprintln!("  {}. {choice}", index + 1);
    }
    eprint!("Number or Enter to cancel: ");
    io::stderr().flush().map_err(|e| e.to_string())?;
    let mut input = String::new();
    io::stdin()
        .read_line(&mut input)
        .map_err(|e| e.to_string())?;
    let selection = input
        .trim()
        .parse::<usize>()
        .ok()
        .and_then(|index| index.checked_sub(1))
        .and_then(|index| choices.get(index));
    selection
        .cloned()
        .ok_or_else(|| format!("no {kind} selected"))
}

pub fn run(session: Session, sync_editor: impl Fn(&Session) -> Result<()> + Send) -> Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(
            "cargo nxrs config needs a terminal; use config check, show, or explain for scripts"
                .into(),
        );
    }
    let session = Arc::new(session);
    let (sender, work) = mpsc::channel();
    let (results, receiver) = mpsc::channel();
    let mut worker_session = Arc::clone(&session);
    thread::scope(|scope| {
        scope.spawn(move || {
            let mut inspection = nxrs_build::nuttx::Inspection::new();
            while let Ok(mut request) = work.recv() {
                // Keep the newest queued revision when resolution or input bursts overlap.
                if matches!(request, Work::Preview(..)) {
                    while let Ok(next) = work.try_recv() {
                        request = next;
                        if !matches!(request, Work::Preview(..)) {
                            break;
                        }
                    }
                }
                let done = match request {
                    Work::Preview(revision, draft) => {
                        let preview = worker_session
                            .preview(&draft, &mut inspection)
                            .unwrap_or_else(problem);
                        Resolved::Preview(revision, preview)
                    }
                    Work::Save(revision, preview, retry) => {
                        let (result, published) = match if retry {
                            Ok(true)
                        } else {
                            worker_session.publish(&preview)
                        } {
                            Ok(true) => {
                                let result = worker_session
                                    .sync(&preview, &mut inspection)
                                    .and_then(|_| sync_editor(&worker_session))
                                    .and_then(|_| worker_session.refresh(&preview))
                                    .map(Arc::new);
                                if let Ok(session) = &result {
                                    worker_session = Arc::clone(session);
                                }
                                (result, true)
                            }
                            Ok(false) => (Ok(Arc::clone(&worker_session)), false),
                            Err(error) => (Err(error), false),
                        };
                        Resolved::Save {
                            revision,
                            preview,
                            result,
                            published,
                        }
                    }
                };
                if results.send(done).is_err() {
                    break;
                }
            }
        });
        let mut screen = Screen {
            session,
            sender,
            draft: Draft::default(),
            revision: 0,
            pending: false,
            saving: false,
            retry: false,
            preview: None,
            baseline: None,
            last_resolved: None,
            filter: String::new(),
            group: Vec::new(),
            changed_only: false,
            technical: false,
            selected: 0,
            pane: Pane::List,
            editor: None,
            input: String::new(),
            message: String::new(),
            save_error: None,
            scroll: 0,
            history: Vec::new(),
        };
        screen.resolve()?;
        terminal::enable_raw_mode().map_err(|e| e.to_string())?;
        let restore = Restore;
        let previous_hook = Arc::new(std::panic::take_hook());
        let restore_hook = Arc::clone(&previous_hook);
        std::panic::set_hook(Box::new(move |info| {
            let _ = terminal::disable_raw_mode();
            let _ = execute!(io::stdout(), LeaveAlternateScreen, crossterm::cursor::Show);
            restore_hook(info);
        }));
        let result = (|| {
            execute!(io::stdout(), EnterAlternateScreen).map_err(|e| e.to_string())?;
            let mut terminal =
                Terminal::new(CrosstermBackend::new(io::stdout())).map_err(|e| e.to_string())?;
            loop {
                let mut completed = false;
                while let Ok(done) = receiver.try_recv() {
                    screen.accept(done);
                    completed = true;
                }
                if completed {
                    // Native preparation may write progress to the terminal.
                    // Repaint after it finishes instead of retaining stale cells.
                    terminal.clear().map_err(|e| e.to_string())?;
                }
                terminal
                    .draw(|frame| screen.render(frame))
                    .map_err(|e| e.to_string())?;
                if event::poll(Duration::from_millis(100)).map_err(|e| e.to_string())? {
                    if let Event::Key(key) = event::read().map_err(|e| e.to_string())? {
                        if key.kind != KeyEventKind::Release && screen.key(key)? {
                            return Ok(());
                        }
                    }
                }
            }
        })();
        drop(restore);
        std::panic::set_hook(Box::new(move |info| previous_hook(info)));
        // Drop the input sender before the scoped worker joins.
        drop(screen);
        result
    })
}

impl Screen {
    fn resolve(&mut self) -> Result<()> {
        self.revision += 1;
        self.pending = true;
        self.message.clear();
        self.save_error = None;
        self.sender
            .send(Work::Preview(self.revision, self.draft.clone()))
            .map_err(|e| e.to_string())
    }

    fn accept(&mut self, done: Resolved) {
        match done {
            Resolved::Preview(revision, preview) if revision == self.revision => {
                let selected = self.selected_name();
                self.pending = false;
                self.message.clear();
                if preview.report["symbols"].is_object() {
                    if self.baseline.is_none() {
                        self.baseline = Some(preview.report.clone());
                    }
                    self.last_resolved = Some(preview.report["symbols"].clone());
                }
                self.preview = Some(preview);
                self.preserve_selection(selected.as_deref());
                if self.last_resolved.is_none() && !self.problems().is_empty() {
                    self.pane = Pane::Detail;
                }
            }
            Resolved::Save {
                revision,
                mut preview,
                result,
                published,
            } if revision == self.revision => {
                let selected = self.selected_name();
                self.saving = false;
                match result {
                    Ok(session) => {
                        self.session = session;
                        self.retry = false;
                        self.save_error = None;
                        self.draft = Draft::default();
                        // A saved request is no longer in Changed. Return to
                        // browsing instead of treating its removal as hidden.
                        self.changed_only = false;
                        self.history.clear();
                        self.baseline = Some(preview.report.clone());
                        self.last_resolved = Some(preview.report["symbols"].clone());
                        preview.edits.clear();
                        preview.authored.clear();
                        self.message = if published {
                            "Synced. Continue editing with Tab or Escape."
                        } else {
                            "No source changes."
                        }
                        .into();
                    }
                    Err(error) if published => {
                        self.retry = true;
                        self.save_error = Some(error);
                        self.message =
                            "Saved; sync failed. Review the error, then press S to retry.".into();
                    }
                    Err(error) => {
                        self.save_error = Some(error);
                        self.message = "Save failed. Review the error below.".into();
                    }
                }
                self.preview = Some(preview);
                if let Some(name) = selected {
                    if let Some(index) =
                        self.names().iter().position(|candidate| *candidate == name)
                    {
                        self.selected = index;
                    } else if self.catalog().get(&name).is_some() {
                        // Changed includes unavailable settings. Preserve their
                        // identity after saving even though the filter is cleared.
                        let pane = self.pane;
                        self.focus_symbol(&name);
                        self.pane = pane;
                    } else {
                        self.selected = 0;
                    }
                } else {
                    self.selected = 0;
                }
            }
            _ => {}
        }
    }

    fn catalog(&self) -> &Value {
        self.preview
            .as_ref()
            .filter(|p| p.report["symbols"].is_object())
            .map(|p| &p.report["symbols"])
            .or(self.last_resolved.as_ref())
            .unwrap_or(&Value::Null)
    }

    fn problems(&self) -> &[Value] {
        self.preview
            .as_ref()
            .and_then(|preview| preview.report["problems"].as_array())
            .map(Vec::as_slice)
            .unwrap_or_default()
    }

    fn stale_catalog(&self) -> bool {
        self.last_resolved.is_some()
            && !self.saving
            && (self.pending
                || self
                    .preview
                    .as_ref()
                    .is_none_or(|preview| !preview.report["symbols"].is_object()))
    }

    fn selected_name(&self) -> Option<String> {
        self.names()
            .get(self.selected)
            .map(|name| (*name).to_owned())
    }

    fn requested(&self, name: &str) -> bool {
        self.draft.symbols.contains_key(name) || self.draft.pins.contains_key(name)
    }

    fn has_unsaved_edits(&self) -> bool {
        if self.retry {
            // Source publication succeeded; exiting cannot discard it.
            return false;
        }
        self.preview.as_ref().is_some_and(|p| !p.edits.is_empty())
            || ((self.pending
                || self
                    .preview
                    .as_ref()
                    .is_none_or(|p| !p.report["symbols"].is_object()))
                && (!self.draft.symbols.is_empty() || !self.draft.pins.is_empty()))
    }

    fn remember(&mut self, name: String) {
        self.history.push(Navigation {
            filter: self.filter.clone(),
            group: self.group.clone(),
            changed_only: self.changed_only,
            selected: name,
        });
    }

    fn focus_symbol(&mut self, name: &str) {
        self.filter = format!("CONFIG_{name}");
        self.group.clear();
        self.changed_only = false;
        self.selected = 0;
        self.pane = Pane::Detail;
        self.scroll = 0;
    }

    fn groups(&self) -> Vec<Vec<String>> {
        let mut groups = BTreeSet::from([Vec::new()]);
        for symbol in self
            .catalog()
            .as_object()
            .into_iter()
            .flat_map(|s| s.values())
        {
            for path in menu_paths(symbol) {
                for depth in 1..=path.len() {
                    groups.insert(path[..depth].to_vec());
                }
            }
        }
        groups.into_iter().collect()
    }

    fn preserve_selection(&mut self, selected: Option<&str>) {
        let Some(name) = selected else {
            self.selected = self.selected.min(self.names().len().saturating_sub(1));
            return;
        };
        if let Some(index) = self.names().iter().position(|candidate| *candidate == name) {
            self.selected = index;
        } else if self.catalog().get(name).is_some() {
            // An edited prerequisite can hide the current row. Keep its detail
            // visible so the next key cannot silently operate on another setting.
            self.remember(name.to_owned());
            let pane = self.pane;
            self.focus_symbol(name);
            if matches!(pane, Pane::Review) {
                self.pane = Pane::Review;
            }
            self.message = "This setting is now unavailable; its details remain available.".into();
        } else {
            self.selected = self.selected.min(self.names().len().saturating_sub(1));
        }
    }

    fn names(&self) -> Vec<&str> {
        let Some(symbols) = self.catalog().as_object() else {
            return vec![];
        };
        let query = self.filter.to_lowercase();
        let exact = query.strip_prefix("config_").unwrap_or(&query);
        let exact_match = (!exact.is_empty())
            .then(|| symbols.get_key_value(&exact.to_uppercase()))
            .flatten()
            .map(|(name, _)| name.as_str());
        symbols
            .iter()
            .filter(|(name, value)| {
                (self.group.is_empty()
                    || menu_paths(value)
                        .iter()
                        .any(|path| path.starts_with(&self.group)))
                    && (!self.changed_only || self.requested(name))
                    && if let Some(exact) = exact_match {
                        name.as_str() == exact
                    } else if query.is_empty() {
                        value["visibility"] != "n" || !self.group.is_empty() || self.changed_only
                    } else {
                        name.to_lowercase().contains(exact)
                            || prompt(value).to_lowercase().contains(&query)
                    }
            })
            .map(|(name, _)| name.as_str())
            .collect()
    }

    fn save(&mut self) -> Result<()> {
        self.scroll = 0;
        if !matches!(self.pane, Pane::Review) {
            self.pane = Pane::Review;
            self.message = "Review the edits, then press S to save and sync.".into();
            return Ok(());
        }
        if self.pending || self.saving {
            return Ok(());
        }
        let Some(preview) = self.preview.take() else {
            return Ok(());
        };
        if let Err(error) = nxrs_build::nuttx_config::check_report(&preview.report) {
            self.message = error
                .lines()
                .next()
                .unwrap_or("Resolve the draft before saving.")
                .into();
            self.preview = Some(preview);
            return Ok(());
        }
        self.saving = true;
        self.sender
            .send(Work::Save(self.revision, preview, self.retry))
            .map_err(|e| e.to_string())
    }

    fn edit(&mut self, name: String, pin: bool) -> Result<()> {
        if self.pending {
            self.message =
                "Wait for the current edit to resolve; navigation is still available.".into();
            return Ok(());
        }
        if self.saving || self.retry || self.baseline.is_none() {
            return Ok(());
        }
        let symbol = &self.catalog()[&name];
        if pin {
            self.input = self
                .draft
                .pins
                .get(&name)
                .copied()
                .map(|value| value.to_string())
                .unwrap_or_else(|| value_string(&symbol["effective"]).to_owned());
            self.editor = Some(Editor::Value { name, pin: true });
        } else if symbol.get("ownership").is_some() || symbol["visibility"] == "n" {
            self.message = "Read Owned by or Requires in the detail pane.".into();
        } else if let Some(alternatives) = symbol["choice"]["alternatives"].as_array() {
            let options = alternatives
                .iter()
                .filter_map(Value::as_str)
                .filter(|key| {
                    self.catalog()[*key]["visibility"] != "n"
                        && self.catalog()[*key].get("ownership").is_none()
                })
                .map(str::to_owned)
                .collect::<Vec<_>>();
            let selected = options
                .iter()
                .position(|key| symbol["choice"]["selected"] == key.as_str())
                .unwrap_or(0);
            self.editor = Some(Editor::Choose {
                options,
                selected,
                prerequisite: false,
            });
        } else if let Some(values) = symbol["assignable"]
            .as_array()
            .filter(|values| !values.is_empty())
        {
            let current = &symbol["effective"];
            if values.len() < 2 {
                self.message =
                    "Only one value is assignable. Review Enabled by and Requires.".into();
                return Ok(());
            }
            let next = values
                .iter()
                .position(|v| v == current)
                .map_or(0, |index| (index + 1) % values.len());
            self.draft
                .symbols
                .insert(name, Some(value_string(&values[next]).to_owned()));
            self.resolve()?;
        } else {
            self.input = value_string(&symbol["effective"]).into();
            self.editor = Some(Editor::Value { name, pin: false });
        }
        Ok(())
    }

    fn edited_input(&mut self, editor: Editor, key: KeyEvent) -> Result<()> {
        if key.code == KeyCode::Esc {
            return Ok(());
        }
        match editor {
            Editor::Quit => unreachable!("quit confirmation is handled before text input"),
            Editor::Groups {
                options,
                mut selected,
            } => {
                if key.code == KeyCode::Enter {
                    if let Some(group) = matching_groups(&options, &self.input).get(selected) {
                        self.group = (*group).clone();
                        self.filter.clear();
                        self.changed_only = false;
                        self.history.clear();
                        self.selected = 0;
                        self.scroll = 0;
                        self.pane = Pane::List;
                        self.message.clear();
                    } else {
                        self.editor = Some(Editor::Groups {
                            options,
                            selected: 0,
                        });
                    }
                    return Ok(());
                }
                match key.code {
                    KeyCode::Backspace => {
                        self.input.pop();
                        selected = 0;
                    }
                    KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.input.clear();
                        selected = 0;
                    }
                    KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.input.push(c);
                        selected = 0;
                    }
                    _ => {
                        selected = move_index(
                            selected,
                            matching_groups(&options, &self.input).len(),
                            key.code,
                        )
                    }
                }
                self.editor = Some(Editor::Groups { options, selected });
            }
            Editor::Choose {
                options,
                mut selected,
                prerequisite,
            } => {
                if key.code == KeyCode::Enter {
                    if let Some(name) = options.get(selected) {
                        if prerequisite {
                            if let Some(current) = self.selected_name() {
                                self.remember(current);
                            }
                            self.focus_symbol(name);
                        } else {
                            if self.catalog()[name]["choice"]["selected"] == name.as_str() {
                                return Ok(());
                            }
                            let previous = self.selected_name();
                            for alternative in self.catalog()[name]["choice"]["alternatives"]
                                .as_array()
                                .into_iter()
                                .flatten()
                                .filter_map(Value::as_str)
                                .map(str::to_owned)
                                .collect::<Vec<_>>()
                            {
                                if alternative != *name {
                                    self.draft.symbols.insert(alternative, None);
                                }
                            }
                            self.draft.symbols.insert(name.clone(), Some("y".into()));
                            // Changed gains the other alternatives immediately;
                            // keep focus by name before resolving the new draft.
                            let names = self.names();
                            self.selected = names
                                .iter()
                                .position(|candidate| *candidate == name)
                                .or_else(|| {
                                    previous.and_then(|previous| {
                                        names.iter().position(|candidate| *candidate == previous)
                                    })
                                })
                                .unwrap_or(0);
                            self.resolve()?;
                        }
                    }
                    self.message.clear();
                    return Ok(());
                }
                selected = move_index(selected, options.len(), key.code);
                self.editor = Some(Editor::Choose {
                    options,
                    selected,
                    prerequisite,
                });
            }
            editor => {
                match key.code {
                    KeyCode::Enter => {
                        match editor {
                            Editor::Search => {
                                self.filter = self.input.trim().to_owned();
                                self.group.clear();
                                self.changed_only = false;
                                self.history.clear();
                                self.selected = 0;
                                self.pane = Pane::List;
                                self.scroll = 0;
                                self.message.clear();
                            }
                            Editor::Value { name, pin } => {
                                if pin {
                                    let Ok(value) = self.input.parse::<u64>() else {
                                        self.message = "Enter an unsigned pin number.".into();
                                        self.editor = Some(Editor::Value { name, pin });
                                        return Ok(());
                                    };
                                    self.draft.pins.insert(name, value);
                                } else {
                                    let value = if self.catalog()[&name]["type"] == "string" {
                                        format!(
                                            "\"{}\"",
                                            self.input.replace('\\', "\\\\").replace('"', "\\\"")
                                        )
                                    } else {
                                        self.input.clone()
                                    };
                                    self.draft.symbols.insert(name, Some(value));
                                }
                                self.resolve()?;
                            }
                            _ => unreachable!(),
                        }
                        return Ok(());
                    }
                    KeyCode::Backspace => {
                        self.input.pop();
                    }
                    KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.input.clear()
                    }
                    KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.input.push(c)
                    }
                    _ => {}
                }
                self.editor = Some(editor);
            }
        }
        Ok(())
    }

    fn key(&mut self, key: KeyEvent) -> Result<bool> {
        if matches!(self.editor, Some(Editor::Quit)) {
            match key.code {
                KeyCode::Char('y' | 'Y') => return Ok(true),
                KeyCode::Esc | KeyCode::Enter | KeyCode::Char('n' | 'N') => self.editor = None,
                _ => {}
            }
            return Ok(false);
        }
        if let Some(editor) = self.editor.take() {
            self.edited_input(editor, key)?;
            return Ok(false);
        }
        if self.saving {
            return Ok(false);
        }
        match key.code {
            KeyCode::Char('q' | 'c')
                if key.code == KeyCode::Char('q')
                    || key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                if !self.has_unsaved_edits() {
                    return Ok(true);
                }
                self.editor = Some(Editor::Quit);
            }
            KeyCode::Esc => {
                if matches!(self.pane, Pane::Review) {
                    self.pane = Pane::Detail;
                } else if let Some(previous) = self.history.pop() {
                    self.filter = previous.filter;
                    self.group = previous.group;
                    self.changed_only = previous.changed_only;
                    self.selected = self
                        .names()
                        .iter()
                        .position(|name| *name == previous.selected)
                        .unwrap_or(0);
                    if !self.names().iter().any(|name| *name == previous.selected) {
                        self.pane = Pane::List;
                    }
                } else if !matches!(self.pane, Pane::List) {
                    self.pane = Pane::List;
                } else if !self.filter.is_empty() || !self.group.is_empty() || self.changed_only {
                    let selected = self.selected_name();
                    self.filter.clear();
                    self.group.clear();
                    self.changed_only = false;
                    self.selected = selected
                        .and_then(|name| {
                            self.names().iter().position(|candidate| *candidate == name)
                        })
                        .unwrap_or(0);
                } else {
                    self.message = "At all settings. Press q to quit.".into();
                }
                self.scroll = 0;
            }
            KeyCode::Char('/') => {
                self.input.clear();
                self.editor = Some(Editor::Search);
            }
            KeyCode::Char('g') => {
                let options = self.groups();
                let selected = options
                    .iter()
                    .position(|group| *group == self.group)
                    .unwrap_or(0);
                self.input.clear();
                self.editor = Some(Editor::Groups { options, selected });
            }
            KeyCode::Char('c') => {
                self.changed_only = !self.changed_only;
                self.filter.clear();
                self.group.clear();
                self.history.clear();
                self.selected = 0;
                self.scroll = 0;
                self.pane = Pane::List;
                self.message.clear();
            }
            KeyCode::Char('t') if !matches!(self.pane, Pane::Review) => {
                self.technical = !self.technical;
                self.pane = Pane::Detail;
                self.scroll = 0;
            }
            KeyCode::Char('S') => self.save()?,
            KeyCode::Char('r') => {
                self.pane = Pane::Review;
                self.scroll = 0;
            }
            KeyCode::Tab | KeyCode::BackTab => {
                self.pane = match self.pane {
                    Pane::List => Pane::Detail,
                    Pane::Detail => Pane::List,
                    Pane::Review => Pane::List,
                };
                self.scroll = 0;
            }
            KeyCode::Up | KeyCode::Down | KeyCode::PageUp | KeyCode::PageDown => {
                if matches!(self.pane, Pane::List) {
                    self.selected = move_index(self.selected, self.names().len(), key.code);
                } else {
                    let delta = if matches!(key.code, KeyCode::PageDown | KeyCode::PageUp) {
                        12
                    } else {
                        1
                    };
                    self.scroll = if matches!(key.code, KeyCode::Down | KeyCode::PageDown) {
                        self.scroll.saturating_add(delta)
                    } else {
                        self.scroll.saturating_sub(delta)
                    };
                }
            }
            KeyCode::Enter | KeyCode::Char(' ') | KeyCode::Char('d') | KeyCode::Char('p')
                if !matches!(self.pane, Pane::Review) =>
            {
                if let Some(name) = self
                    .names()
                    .get(self.selected)
                    .map(|name| (*name).to_owned())
                {
                    match key.code {
                        KeyCode::Enter if matches!(self.pane, Pane::List) => {
                            self.pane = Pane::Detail;
                            self.scroll = 0;
                        }
                        KeyCode::Char('d') => {
                            if self.retry {
                                self.message =
                                    "Sources are saved. Retry sync in Review before editing."
                                        .into();
                            } else if self.pending {
                                self.message =
                                    "Wait for the current edit to resolve before resetting.".into();
                            } else {
                                if self.session.pin_owner(&name).is_ok() {
                                    self.draft.pins.remove(&name);
                                    if !self.names().iter().any(|candidate| *candidate == name) {
                                        self.remember(name.clone());
                                        self.focus_symbol(&name);
                                    }
                                } else {
                                    self.draft.symbols.insert(name, None);
                                }
                                self.resolve()?;
                            }
                        }
                        KeyCode::Char('p') => {
                            let options = self.catalog()[&name]["prerequisites"]
                                .as_array()
                                .into_iter()
                                .flatten()
                                .filter_map(Value::as_str)
                                .map(str::to_owned)
                                .collect::<Vec<_>>();
                            if !options.is_empty() {
                                self.editor = Some(Editor::Choose {
                                    options,
                                    selected: 0,
                                    prerequisite: true,
                                });
                            }
                        }
                        KeyCode::Enter | KeyCode::Char(' ')
                            if !matches!(self.pane, Pane::Review) =>
                        {
                            let pin = self.session.pin_owner(&name).is_ok();
                            self.edit(name, pin)?;
                        }
                        _ => {}
                    }
                }
            }
            _ => {}
        }
        Ok(false)
    }

    fn render(&mut self, frame: &mut Frame) {
        let areas = Layout::vertical([
            Constraint::Length(2),
            Constraint::Min(5),
            Constraint::Length(3),
        ])
        .split(frame.area());
        let board = value_string(&self.session.profile.data["board"]);
        let counts = self
            .preview
            .as_ref()
            .map(|preview| {
                format!(
                    "Source edits: {} · Native changes: {} · Problems: {}",
                    preview.authored.len(),
                    presentation::native_change_count(self.baseline.as_ref(), &preview.report),
                    self.problems().len() + usize::from(self.save_error.is_some()),
                )
            })
            .unwrap_or_default();
        let status = if self.saving {
            "Saving and preparing"
        } else if self.pending {
            if self.last_resolved.is_some() {
                "Resolving draft · showing last resolved values"
            } else {
                "Resolving configuration"
            }
        } else if self.retry {
            "Saved; sync failed"
        } else if self.save_error.is_some() {
            "Save failed · r Review problems"
        } else if !self.problems().is_empty() {
            if self.stale_catalog() {
                "Resolution failed · showing last resolved values · r Review"
            } else if self.last_resolved.is_none() {
                "Configuration unavailable · r Review problems"
            } else {
                "Draft invalid · r Review problems"
            }
        } else if self.draft.symbols.is_empty() && self.draft.pins.is_empty() {
            "No pending edits"
        } else {
            &counts
        };
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(vec![
                    Span::styled("NXRS config", Style::default().add_modifier(Modifier::BOLD)),
                    Span::raw(format!(
                        " · {} / {} · {board}",
                        self.session.app.bin,
                        self.session.profile.name()
                    )),
                ]),
                Line::raw(status.to_owned()),
            ]),
            areas[0],
        );
        let body = areas[1];
        let detail_title = if self.stale_catalog() {
            "Setting · last resolved values"
        } else {
            "Setting"
        };
        if self.last_resolved.is_none() && !self.problems().is_empty() {
            let problems = self
                .problems()
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join("\n\n");
            self.paragraph(frame, body, "Configuration unavailable", problems, true);
        } else if self.saving {
            self.paragraph(frame, body, "Save & sync", "Saving reviewed sources and preparing build inputs…\n\nPlease wait for the result before continuing.".into(), true);
        } else if matches!(self.pane, Pane::Review) {
            self.paragraph(frame, body, "Review", self.review(), true);
        } else if frame.area().width >= 105 {
            let columns = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Percentage(43), Constraint::Percentage(57)])
                .split(body);
            self.list(frame, columns[0]);
            self.paragraph(
                frame,
                columns[1],
                detail_title,
                self.detail(),
                matches!(self.pane, Pane::Detail),
            );
        } else if matches!(self.pane, Pane::List) {
            self.list(frame, body);
        } else {
            self.paragraph(frame, body, detail_title, self.detail(), true);
        }
        let shortcuts = self.shortcuts();
        let status = self
            .message
            .lines()
            .next()
            .filter(|line| !line.is_empty())
            .or_else(|| self.problems().iter().find_map(Value::as_str))
            .unwrap_or("");
        let status = if self.saving || self.editor.is_some() {
            ""
        } else {
            status
        };
        frame.render_widget(Paragraph::new(format!("{shortcuts}\n{status}")), areas[2]);
        if let Some(editor) = &self.editor {
            let width = body.width.saturating_sub(8).min(74);
            let height = body.height.min(12);
            let popup = Rect::new(
                body.x + (body.width - width) / 2,
                body.y + (body.height - height) / 2,
                width,
                height,
            );
            frame.render_widget(ratatui::widgets::Clear, popup);
            let (title, contents) = match editor {
                Editor::Quit => (
                    "Discard unsaved edits?",
                    "Your requested source changes have not been saved.\n\nEnter / Esc  Keep editing\ny            Discard and quit".to_owned(),
                ),
                Editor::Search => (
                    "Search all native settings",
                    format!("{}▏\n\nSearch prompts or CONFIG_ names.\nEnter Search · Esc Cancel", self.input),
                ),
                Editor::Groups { options, selected } => {
                    let visible = matching_groups(options, &self.input);
                    let count = height.saturating_sub(5).max(1) as usize;
                    let offset = selected.saturating_sub(count.saturating_sub(1));
                    let rows = visible.iter().enumerate().skip(offset).take(count).map(|(index, path)| {
                        format!("{} {}", if index == *selected { ">" } else { " " }, group_label(path))
                    }).collect::<Vec<_>>().join("\n");
                    let rows = if rows.is_empty() { "No matching groups.".to_owned() } else { rows };
                    (
                        "Native groups · type to filter",
                        format!("{}▏\n{rows}\n↑↓ Choose · Enter Browse · Esc Cancel", self.input),
                    )
                },
                Editor::Value { name, .. } => (
                    "Edit value",
                    format!(
                        "CONFIG_{name}\n{}▏\nEnter Preview · Ctrl-U Clear · Esc Cancel",
                        self.input
                    ),
                ),
                Editor::Choose {
                    options, selected, ..
                } => (
                    "Choose with ↑↓ and Enter",
                    options
                        .iter()
                        .enumerate()
                        .skip(selected.saturating_sub(height.saturating_sub(3) as usize))
                        .take(height.saturating_sub(2) as usize)
                        .map(|(index, name)| {
                            let symbol = &self.catalog()[name];
                            let state = if symbol.get("ownership").is_some() {
                                " · owned"
                            } else if symbol["visibility"] == "n" {
                                " · unavailable"
                            } else {
                                ""
                            };
                            format!(
                                "{} [{}{}] {} · CONFIG_{}",
                                if index == *selected { ">" } else { " " },
                                value_string(&symbol["effective"]),
                                state,
                                prompt(symbol),
                                name,
                            )
                        })
                        .collect::<Vec<_>>()
                        .join("\n"),
                ),
            };
            frame.render_widget(
                Paragraph::new(contents).block(pane_block(title, true)),
                popup,
            );
        }
    }

    fn list(&self, frame: &mut Frame, area: Rect) {
        let active = matches!(self.pane, Pane::List) && self.editor.is_none();
        let names = self.names();
        let offset = self
            .selected
            .saturating_sub(area.height.saturating_sub(3) as usize);
        let rows = names
            .iter()
            .enumerate()
            .skip(offset)
            .take(area.height.saturating_sub(2) as usize)
            .map(|(index, name)| {
                let symbol = &self.catalog()[*name];
                let marker = if index == self.selected { ">" } else { " " };
                let changed = if self.requested(name) { "*" } else { " " };
                let disposition = if symbol.get("ownership").is_some() {
                    " owned"
                } else if symbol["visibility"] == "n" {
                    " unavailable"
                } else {
                    ""
                };
                let label = prompt(symbol);
                let effective = match (symbol["type"].as_str(), symbol["effective"].as_str()) {
                    (Some("bool" | "tristate"), Some("y")) => "On",
                    (Some("bool" | "tristate"), Some("n")) => "Off",
                    (Some("tristate"), Some("m")) => "Module",
                    _ => value_string(&symbol["effective"]),
                };
                let short_value = if effective.chars().count() > 12 {
                    format!("{}…", effective.chars().take(11).collect::<String>())
                } else {
                    effective.to_owned()
                };
                let row = Line::from(vec![
                    Span::raw(format!("{marker}{changed} [{short_value}{disposition}] ")),
                    Span::raw(if label.is_empty() {
                        format!("CONFIG_{name}")
                    } else {
                        label.to_owned()
                    }),
                    Span::styled(
                        if label.is_empty() {
                            String::new()
                        } else {
                            format!(" · CONFIG_{name}")
                        },
                        Style::default().add_modifier(Modifier::DIM),
                    ),
                ]);
                if index == self.selected {
                    row.style(Style::default().add_modifier(if active {
                        Modifier::REVERSED
                    } else {
                        Modifier::BOLD
                    }))
                } else {
                    row
                }
            })
            .collect::<Vec<_>>();
        let mut title = if self.changed_only {
            format!("Changed ({}) · * requested edit", names.len())
        } else if !self.group.is_empty() {
            format!("{} ({})", self.group.last().unwrap(), names.len())
        } else if self.filter.is_empty() {
            format!("Settings ({})", names.len())
        } else {
            format!("Search {} ({})", self.filter, names.len())
        };
        if self.stale_catalog() {
            title.push_str(" · last resolved");
        }
        let block = pane_block(&title, active);
        if names.is_empty() {
            let message = if self.changed_only {
                "No requested edits.\n\nc or Esc  All settings"
            } else {
                "No settings match this view.\n\n/ Search all settings\ng Browse native groups\nEsc Clear filters"
            };
            frame.render_widget(Paragraph::new(message).block(block), area);
            return;
        }
        frame.render_widget(List::new(rows).block(block), area);
    }

    fn paragraph(
        &mut self,
        frame: &mut Frame,
        area: Rect,
        title: &str,
        body: String,
        active: bool,
    ) {
        let block = pane_block(title, active && self.editor.is_none());
        let inner = block.inner(area);
        let lines = body
            .lines()
            .enumerate()
            .map(|(index, line)| {
                let prominent = index == 0
                    || matches!(
                        line,
                        "Help"
                            | "Problems to resolve"
                            | "Your source edits"
                            | "Saved source edits"
                            | "Resulting native changes"
                    );
                Line::styled(
                    line.to_owned(),
                    if prominent {
                        Style::default().add_modifier(Modifier::BOLD)
                    } else {
                        Style::default()
                    },
                )
            })
            .collect::<Vec<_>>();
        let paragraph = Paragraph::new(Text::from(lines)).wrap(Wrap { trim: false });
        // Measure without the block using its inner width, so borders are
        // accounted for exactly once and scroll is in wrapped display rows.
        let maximum = paragraph
            .line_count(inner.width)
            .saturating_sub(inner.height as usize);
        self.scroll = self.scroll.min(maximum.min(u16::MAX as usize) as u16);
        frame.render_widget(paragraph.scroll((self.scroll, 0)).block(block), area);
    }

    fn detail(&self) -> String {
        let Some(name) = self.names().get(self.selected).copied() else {
            return "Search for a native setting with /.".into();
        };
        presentation::detail(
            name,
            self.catalog(),
            &self.draft,
            self.technical,
            self.session.pin_owner(name).is_ok(),
        )
    }

    fn shortcuts(&self) -> &'static str {
        if self.saving {
            return "Saving and preparing… Please wait.\nSources and build inputs are being synchronized.";
        }
        if let Some(editor) = &self.editor {
            return match editor {
                Editor::Quit => "y Discard and quit  Enter/Esc Keep editing\nPreviously saved changes remain.",
                Editor::Groups { .. } => "Type Filter groups  ↑↓ Move  Enter Browse\nCtrl-U Clear  Esc Cancel",
                Editor::Search => "Type Search prompts or CONFIG_ names\nEnter Search all settings  Ctrl-U Clear  Esc Cancel",
                Editor::Value { .. } => "Type Native value  Enter Preview\nCtrl-U Clear  Esc Cancel",
                Editor::Choose { prerequisite: true, .. } => "↑↓ Move  Enter Inspect prerequisite\nEsc Cancel",
                Editor::Choose { prerequisite: false, .. } => "↑↓ Move  Enter Select value\nEsc Cancel",
            };
        }
        match self.pane {
            Pane::Review if self.retry => "↑↓ Scroll  S Retry sync\nTab Settings  Esc Back  q Quit",
            Pane::Review if self.pending => "↑↓ Scroll  Resolving draft · Save unavailable\nTab Settings  Esc Back  q Quit",
            Pane::Review if !self.problems().is_empty() => "↑↓ Scroll  Resolve problems before saving\nTab Settings  Esc Back  q Quit",
            Pane::Review => "↑↓ Scroll  S Save & sync\nTab Settings  Esc Back  q Quit",
            Pane::List if self.pending || self.retry || self.baseline.is_none() => "/ Search  g Groups  c Changed  ↑↓ Move  Enter Details\nTab Focus  r/S Review changes  Esc Back  q Quit",
            Pane::List => "/ Search  g Groups  c Changed  ↑↓ Move  Enter Details  Space Edit\nTab Focus  p Prerequisites  r/S Review changes  Esc Back  q Quit",
            Pane::Detail if self.pending || self.retry || self.baseline.is_none() => "↑↓ Scroll  p Prerequisites  t Technical details\nTab Settings  / Search  g Groups  c Changed  r/S Review  Esc Back  q Quit",
            Pane::Detail => "↑↓ Scroll  Enter/Space Edit  p Prerequisites  d Reset  t Technical\nTab Settings  / Search  g Groups  c Changed  r/S Review  Esc Back  q Quit",
        }
    }

    fn review(&self) -> String {
        presentation::review(
            self.preview.as_ref(),
            self.baseline.as_ref(),
            self.save_error.as_deref(),
            self.pending,
            self.retry,
        )
    }
}

fn pane_block(title: &str, active: bool) -> Block<'static> {
    let mut style = Style::default();
    if active {
        style = style.add_modifier(Modifier::BOLD);
        if std::env::var_os("NO_COLOR").is_none() {
            style = style.fg(Color::Cyan);
        }
    }
    Block::default()
        .title(Line::styled(
            format!(" {}{title} ", if active { "▶ " } else { "" }),
            style,
        ))
        .borders(Borders::ALL)
        .border_type(ratatui::widgets::BorderType::Rounded)
        .border_style(style)
}

fn menu_paths(symbol: &Value) -> Vec<Vec<String>> {
    symbol["locations"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|node| {
            node["menu"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .collect()
}

fn matching_groups<'a>(options: &'a [Vec<String>], query: &str) -> Vec<&'a Vec<String>> {
    let query = query.to_lowercase();
    options
        .iter()
        .filter(|path| {
            if path.is_empty() {
                "all groups".contains(&query)
            } else {
                path.join(" › ").to_lowercase().contains(&query)
            }
        })
        .collect()
}

fn group_label(path: &[String]) -> String {
    match path.split_last() {
        None => "All groups".into(),
        Some((leaf, [])) => leaf.to_owned(),
        Some((leaf, parents)) => format!("{leaf} · {}", parents.join(" › ")),
    }
}

fn move_index(index: usize, length: usize, key: KeyCode) -> usize {
    match key {
        KeyCode::Up => index.saturating_sub(1),
        KeyCode::PageUp => index.saturating_sub(12),
        KeyCode::Down => (index + 1).min(length.saturating_sub(1)),
        KeyCode::PageDown => (index + 12).min(length.saturating_sub(1)),
        _ => index,
    }
}

fn prompt(symbol: &Value) -> &str {
    symbol["locations"]
        .as_array()
        .into_iter()
        .flatten()
        .find_map(|node| node["prompt"].as_str())
        .unwrap_or("")
}

fn value_string(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

pub fn describe(name: &str, symbol: &Value) -> String {
    let value = value_string(&symbol["effective"]);
    let visibility = match value_string(&symbol["visibility"]) {
        "n" => "no visible prompt",
        "m" => "module",
        _ => "visible",
    };
    let mut lines = vec![
        format!("CONFIG_{name}  {}", prompt(symbol)),
        format!(
            "Effective {value:?} · {} · {visibility}",
            value_string(&symbol["type"])
        ),
    ];
    if let Some(assignable) = symbol["assignable"]
        .as_array()
        .filter(|values| !values.is_empty())
    {
        lines.push(format!(
            "Legal assignments {}",
            assignable
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    if let Some(owner) = symbol["ownership"]["owner"].as_str() {
        lines.push(format!(
            "Owned by {owner} {}",
            value_string(&symbol["ownership"]["source"])
        ));
    }
    let requires = &symbol["requires"];
    if requires["expression"] == "y" {
        lines.push("Requires no additional conditions".into());
    } else {
        lines.push(format!(
            "Requires {} ({})",
            value_string(&requires["expression"]),
            value_string(&requires["value"])
        ));
    }
    let mut potential = Vec::new();
    for (field, label, inactive_label) in [
        ("enabled_by", "Enabled by", "Potential selectors (inactive)"),
        (
            "implied_by",
            "Suggested by",
            "Potential suggestions (inactive)",
        ),
    ] {
        if let Some(entries) = symbol[field].as_array() {
            let mut inactive = Vec::new();
            for entry in entries {
                let active = |value: &Value| matches!(value.as_str(), Some("m" | "y"));
                if !active(&entry["value"]) || !active(&entry["when"]["value"]) {
                    inactive.push(format!(
                        "CONFIG_{} ({}; condition {})",
                        value_string(&entry["symbol"]),
                        value_string(&entry["value"]),
                        value_string(&entry["when"]["value"])
                    ));
                } else {
                    lines.push(format!(
                        "{label} CONFIG_{} ({}) when {} ({})",
                        value_string(&entry["symbol"]),
                        value_string(&entry["value"]),
                        value_string(&entry["when"]["expression"]),
                        value_string(&entry["when"]["value"])
                    ));
                }
            }
            if !inactive.is_empty() {
                potential.push(format!("{inactive_label}: {}", inactive.join(", ")));
            }
        }
    }
    if let Some(defaults) = symbol["defaults"].as_array() {
        for default in defaults {
            lines.push(format!(
                "Default {} when {} ({})",
                value_string(&default["value"]),
                value_string(&default["when"]["expression"]),
                value_string(&default["when"]["value"])
            ));
        }
    }
    if let Some(ranges) = symbol["ranges"].as_array() {
        for range in ranges {
            lines.push(format!(
                "Range {}–{} when {} ({})",
                value_string(&range["min"]),
                value_string(&range["max"]),
                value_string(&range["when"]["expression"]),
                value_string(&range["when"]["value"])
            ));
        }
    }
    if let Some(choice) = symbol.get("choice") {
        let names = choice["alternatives"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(", ");
        lines.push(format!(
            "Choice {} · selected {} · {names}",
            value_string(&choice["name"]),
            value_string(&choice["selected"])
        ));
    }
    if let Some(provenance) = symbol["provenance"].as_array() {
        for (index, row) in provenance.iter().enumerate() {
            lines.push(format!(
                "{} {} · {}{}",
                value_string(&row["kind"]),
                value_string(&row["value"]),
                value_string(&row["source"]),
                if index + 1 < provenance.len() {
                    " (superseded)"
                } else {
                    ""
                }
            ));
        }
    }
    let mut help = Vec::new();
    if let Some(nodes) = symbol["locations"].as_array() {
        for node in nodes {
            let menu = node["menu"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(" › ");
            lines.push(format!(
                "Location {menu}  ({}:{})",
                value_string(&node["file"]),
                node["line"]
            ));
            if let Some(prompt_condition) = node["prompt_condition"]["expression"].as_str() {
                if node["prompt_condition"] != symbol["requires"] {
                    lines.push(format!(
                        "Prompt visible when {prompt_condition} ({})",
                        value_string(&node["prompt_condition"]["value"])
                    ));
                }
            }
            if let Some(text) = node["help"].as_str() {
                help.push(text);
            }
        }
    }
    lines.extend(potential);
    for text in help {
        lines.push(format!("\nHelp\n{text}"));
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use nxrs_build::{
        build::{App, Options},
        profile::Profile,
        TempDir,
    };
    use ratatui::backend::TestBackend;
    use serde_json::json;

    fn screen() -> (Screen, mpsc::Receiver<Work>, TempDir) {
        let temp = TempDir::new(&std::env::temp_dir()).unwrap();
        let root = temp.0.clone();
        let source = root.join("profile.toml");
        std::fs::write(&source, "name = \"fixture\"\nboard = \"sim:nsh\"\n").unwrap();
        let manifest = root.join("Cargo.toml");
        std::fs::write(
            &manifest,
            "[package]\nname = \"fixture\"\nversion = \"0.1.0\"\n",
        )
        .unwrap();
        let app = App::load(&manifest, None).unwrap();
        let profile = Profile::from_data(json!({"name":"fixture","board":"sim:nsh"}), vec![source]);
        let session = Session::new(
            root.clone(),
            app,
            profile,
            root.join("out"),
            Options::default(),
        )
        .unwrap();
        let (sender, receiver) = mpsc::channel();
        (
            Screen {
                session: Arc::new(session),
                sender,
                draft: Draft::default(),
                revision: 1,
                pending: true,
                saving: false,
                retry: false,
                preview: None,
                baseline: None,
                last_resolved: None,
                filter: String::new(),
                group: Vec::new(),
                changed_only: false,
                technical: false,
                selected: 0,
                pane: Pane::List,
                editor: None,
                input: String::new(),
                message: String::new(),
                save_error: None,
                scroll: 0,
                history: Vec::new(),
            },
            receiver,
            temp,
        )
    }

    fn symbol(prompt: &str, effective: &str, visibility: &str) -> Value {
        json!({
            "type":"bool", "effective":effective, "visibility":visibility,
            "assignable":["n","y"],
            "locations":[{"prompt":prompt,"menu":[],"file":"Kconfig","line":1}],
            "requires":{"expression":"y","value":"y"}, "prerequisites":[]
        })
    }

    fn preview(symbols: Value) -> Preview {
        let mut preview = problem(String::new());
        preview.report = json!({"symbols":symbols,"problems":[]});
        preview
    }

    fn render(screen: &mut Screen, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| screen.render(frame)).unwrap();
        terminal.backend().to_string()
    }

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[test]
    fn resolution_preserves_symbol_identity_when_the_visible_list_changes() {
        let (mut screen, requests, _temp) = screen();
        screen.accept(Resolved::Preview(
            1,
            preview(json!({
                "A":symbol("Dependent A","y","y"),
                "B":symbol("Parent B","y","y"), "C":symbol("Unrelated C","n","y")
            })),
        ));
        screen.selected = 1;
        screen.edit("B".into(), false).unwrap();
        assert!(matches!(requests.try_recv().unwrap(), Work::Preview(2, _)));
        screen.accept(Resolved::Preview(
            2,
            preview(json!({
                "A":symbol("Dependent A","n","n"),
                "B":symbol("Parent B","n","y"), "C":symbol("Unrelated C","n","y")
            })),
        ));
        assert_eq!(screen.selected_name().as_deref(), Some("B"));
        assert_eq!(screen.selected, 0);

        // If the focused item itself disappears, keep its unavailable detail.
        screen.accept(Resolved::Preview(
            2,
            preview(json!({
                "A":symbol("Dependent A","y","y"), "B":symbol("Parent B","y","y")
            })),
        ));
        screen.selected = 0;
        screen.accept(Resolved::Preview(
            2,
            preview(json!({
                "A":symbol("Dependent A","n","n"), "B":symbol("Parent B","n","y")
            })),
        ));
        assert_eq!(screen.selected_name().as_deref(), Some("A"));
        assert!(matches!(screen.pane, Pane::Detail));
        assert!(screen.message.contains("now unavailable"));
        screen.key(key(KeyCode::Esc)).unwrap();
        assert!(screen.filter.is_empty());
        assert!(matches!(screen.pane, Pane::List));
    }

    #[test]
    fn initial_errors_are_visible_and_later_errors_label_the_last_resolved_values() {
        let (mut screen, _requests, _temp) = screen();
        screen.accept(Resolved::Preview(
            1,
            problem("missing required tool: olddefconfig".into()),
        ));
        for (width, height) in [(80, 24), (126, 34)] {
            let rendered = render(&mut screen, width, height);
            assert!(rendered.contains("Configuration unavailable"));
            assert!(rendered.contains("missing required tool: olddefconfig"));
            assert!(!rendered.contains("No pending edits"));
        }
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"B":symbol("Parent B","y","y")})),
        ));
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"B":symbol("Parent B","n","y")})),
        ));
        screen.accept(Resolved::Preview(
            1,
            problem("invalid pin collision".into()),
        ));
        let rendered = render(&mut screen, 80, 24);
        assert!(rendered.contains("Resolution failed"));
        assert!(rendered.contains("last resolved values"));
        assert!(rendered.contains("invalid pin collision"));
        assert_eq!(screen.catalog()["B"]["effective"], "n");
        assert_eq!(
            screen.baseline.as_ref().unwrap()["symbols"]["B"]["effective"],
            "y"
        );
    }

    #[test]
    fn inactive_selectors_and_implications_are_potential_relationships() {
        let mut selected = symbol("Derived target", "n", "n");
        selected["enabled_by"] = json!([
            {"symbol":"OFF","value":"n","when":{"expression":"y","value":"y"}},
            {"symbol":"BLOCKED","value":"y","when":{"expression":"CONFIG_GATE","value":"n"}},
            {"symbol":"ON","value":"y","when":{"expression":"y","value":"y"}}
        ]);
        selected["implied_by"] = json!([
            {"symbol":"SUGGESTION","value":"n","when":{"expression":"y","value":"y"}}
        ]);
        let detail = describe("TARGET", &selected);
        assert!(detail.contains("Enabled by CONFIG_ON (y)"));
        assert!(!detail.contains("Enabled by CONFIG_OFF"));
        assert!(!detail.contains("Enabled by CONFIG_BLOCKED"));
        assert!(!detail.contains("Suggested by CONFIG_SUGGESTION"));
        assert!(detail.contains("Potential selectors (inactive): CONFIG_OFF (n; condition y)"));
        assert!(detail.contains("Potential suggestions (inactive): CONFIG_SUGGESTION"));
    }

    #[test]
    fn scrolling_reaches_wrapped_help_and_review_at_both_layout_widths() {
        let (mut screen, _requests, _temp) = screen();
        // Ordinary help lines wrap in the split view. A final marker verifies
        // reachability without reproducing the widget's wrapping algorithm.
        let help = format!("{}\nEND OF HELP", "An ordinary help line explains board initialization and dependency behavior in detail.\n".repeat(40));
        let mut setting = symbol("Long native help", "n", "y");
        setting["locations"][0]["help"] = json!(help);
        screen.accept(Resolved::Preview(1, preview(json!({"LONG":setting}))));
        screen.pane = Pane::Detail;
        for (width, height) in [(80, 24), (105, 24), (126, 34)] {
            screen.scroll = u16::MAX;
            assert!(render(&mut screen, width, height).contains("END OF HELP"));
            let bottom = screen.scroll;
            assert!(bottom > 0 && bottom < u16::MAX);
            screen.key(key(KeyCode::Up)).unwrap();
            render(&mut screen, width, height);
            assert_eq!(screen.scroll, bottom - 1);
        }
        screen.pane = Pane::Review;
        screen.save_error = Some(format!("{}\nEND OF ERROR", "A long native diagnostic includes source paths, requested values, dependencies and effective values.\n".repeat(30)));
        screen.scroll = u16::MAX;
        assert!(render(&mut screen, 80, 24).contains("END OF ERROR"));
    }

    #[test]
    fn save_refreshes_the_session_and_baseline_before_the_next_edit() {
        let (mut screen, requests, _temp) = screen();
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"B":symbol("Parent B","y","y")})),
        ));
        screen.draft.symbols.insert("B".into(), Some("n".into()));
        let mut saved = preview(json!({"B":symbol("Parent B","n","y")}));
        saved.authored.push("saved source edit".into());
        saved.edits.insert(
            screen.session.root.join("fixture.conf"),
            "CONFIG_B=n\n".into(),
        );
        let refreshed = Arc::new(
            Session::new(
                screen.session.root.clone(),
                screen.session.app.clone(),
                screen.session.profile.clone(),
                screen.session.out.clone(),
                screen.session.options.clone(),
            )
            .unwrap(),
        );
        screen.accept(Resolved::Save {
            revision: 1,
            preview: saved,
            result: Ok(Arc::clone(&refreshed)),
            published: true,
        });
        assert!(Arc::ptr_eq(&screen.session, &refreshed));
        assert!(screen.draft.symbols.is_empty());
        assert!(screen.preview.as_ref().unwrap().edits.is_empty());
        assert!(screen.preview.as_ref().unwrap().authored.is_empty());
        assert_eq!(
            screen.baseline.as_ref().unwrap()["symbols"]["B"]["effective"],
            "n"
        );
        screen.edit("B".into(), false).unwrap();
        assert_eq!(screen.draft.symbols["B"].as_deref(), Some("y"));
        assert!(matches!(requests.try_recv().unwrap(), Work::Preview(2, _)));
    }

    #[test]
    fn sync_failure_preserves_the_reviewed_draft_and_session_for_retry() {
        let (mut screen, _requests, _temp) = screen();
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"B":symbol("Parent B","n","y")})),
        ));
        let original = Arc::clone(&screen.session);
        screen.draft.symbols.insert("B".into(), Some("n".into()));
        let mut pending = screen.preview.take().unwrap();
        pending.authored.push("reviewed source edit".into());
        screen.accept(Resolved::Save {
            revision: 1,
            preview: pending,
            result: Err("preparation failed".into()),
            published: true,
        });
        assert!(Arc::ptr_eq(&original, &screen.session));
        assert!(screen.retry);
        assert_eq!(
            screen.preview.as_ref().unwrap().authored,
            ["reviewed source edit"]
        );
        assert!(screen.review().contains("preparation failed"));
        screen.edit("B".into(), false).unwrap();
        assert_eq!(screen.draft.symbols["B"].as_deref(), Some("n"));
    }

    #[test]
    fn repeated_toggles_wait_for_the_current_resolution() {
        let (mut screen, requests, _temp) = screen();
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"B":symbol("Parent B","y","y")})),
        ));
        screen.edit("B".into(), false).unwrap();
        screen.edit("B".into(), false).unwrap();
        assert_eq!(screen.revision, 2);
        assert!(matches!(requests.try_recv().unwrap(), Work::Preview(2, _)));
        assert!(requests.try_recv().is_err());
        assert!(screen.message.contains("Wait for the current edit"));
        screen.accept(Resolved::Preview(
            2,
            preview(json!({"B":symbol("Parent B","n","y")})),
        ));
        screen.edit("B".into(), false).unwrap();
        assert_eq!(screen.draft.symbols["B"].as_deref(), Some("y"));
    }

    #[test]
    fn escape_leaves_review_before_returning_through_prerequisite_history() {
        let (mut screen, _requests, _temp) = screen();
        let mut dependent = symbol("Dependent A", "n", "y");
        dependent["prerequisites"] = json!(["B"]);
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"A":dependent,"B":symbol("Parent B","n","y")})),
        ));
        screen.key(key(KeyCode::Char('p'))).unwrap();
        screen.key(key(KeyCode::Enter)).unwrap();
        assert_eq!(screen.selected_name().as_deref(), Some("B"));
        assert_eq!(screen.history.len(), 1);
        screen.key(key(KeyCode::Char('r'))).unwrap();
        assert!(matches!(screen.pane, Pane::Review));

        screen.key(key(KeyCode::Esc)).unwrap();
        assert!(matches!(screen.pane, Pane::Detail));
        assert_eq!(screen.selected_name().as_deref(), Some("B"));
        assert_eq!(screen.filter, "CONFIG_B");
        assert_eq!(screen.history.len(), 1);

        screen.key(key(KeyCode::Esc)).unwrap();
        assert_eq!(screen.selected_name().as_deref(), Some("A"));
        assert!(screen.filter.is_empty());
        assert!(screen.history.is_empty());
    }

    #[test]
    fn prompts_and_prerequisite_state_are_visible_before_raw_names() {
        let (mut screen, _requests, _temp) = screen();
        let mut parent = symbol("Parent subsystem", "n", "y");
        parent["ownership"] = json!({"owner":"resources.bus","source":"profile.toml"});
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"LONG_NATIVE_SYMBOL_NAME":parent})),
        ));
        let list = render(&mut screen, 80, 24);
        assert!(
            list.find("Parent subsystem").unwrap()
                < list.find("CONFIG_LONG_NATIVE_SYMBOL_NAME").unwrap()
        );
        screen.editor = Some(Editor::Choose {
            options: vec!["LONG_NATIVE_SYMBOL_NAME".into()],
            selected: 0,
            prerequisite: true,
        });
        let popup = render(&mut screen, 80, 24);
        assert!(popup.contains("[n · owned] Parent subsystem"));
    }

    #[test]
    fn search_is_global_and_escape_returns_without_discarding_the_draft() {
        let (mut screen, _requests, _temp) = screen();
        let mut a = symbol("Debug assertions", "y", "y");
        a["locations"][0]["menu"] = json!(["Build", "Debug"]);
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"A":a,"B":symbol("Network support","n","y")})),
        ));
        screen.draft.symbols.insert("A".into(), Some("n".into()));
        screen.group = vec!["Build".into(), "Debug".into()];
        screen.changed_only = true;
        screen.key(key(KeyCode::Char('/'))).unwrap();
        screen.input = "network".into();
        screen.key(key(KeyCode::Esc)).unwrap();
        assert_eq!(screen.group, ["Build", "Debug"]);
        assert!(screen.changed_only);
        screen.key(key(KeyCode::Char('/'))).unwrap();
        screen.input = "network".into();
        screen.key(key(KeyCode::Enter)).unwrap();
        assert_eq!(screen.names(), ["B"]);
        assert!(screen.group.is_empty() && !screen.changed_only);
        assert!(!screen.key(key(KeyCode::Esc)).unwrap());
        assert!(screen.filter.is_empty());
        assert_eq!(screen.names(), ["A", "B"]);
        assert!(!screen.key(key(KeyCode::Esc)).unwrap());
        assert_eq!(screen.draft.symbols["A"].as_deref(), Some("n"));
    }

    #[test]
    fn native_groups_match_all_locations_and_whole_path_components() {
        let (mut screen, _requests, _temp) = screen();
        let mut a = symbol("Shared setting", "n", "n");
        a["locations"] = json!([
            {"prompt":"Shared setting","menu":["System","Logging"]},
            {"prompt":"Alternative location","menu":["Drivers","Serial"]}
        ]);
        let mut b = symbol("Other group", "y", "y");
        b["locations"][0]["menu"] = json!(["Systematic"]);
        screen.accept(Resolved::Preview(1, preview(json!({"A":a,"B":b}))));
        screen.key(key(KeyCode::Char('g'))).unwrap();
        assert!(!screen.shortcuts().contains("q Quit"));
        assert!(screen.shortcuts().contains("Enter Browse"));
        for c in "logging".chars() {
            screen.key(key(KeyCode::Char(c))).unwrap();
        }
        screen.key(key(KeyCode::Enter)).unwrap();
        assert_eq!(screen.group, ["System", "Logging"]);
        assert_eq!(screen.names(), ["A"]); // Hidden members remain discoverable.
        screen.group = vec!["Drivers".into()];
        assert_eq!(screen.names(), ["A"]);
        screen.group = vec!["System".into()];
        assert_eq!(screen.names(), ["A"]); // Systematic is a different group.
        screen.key(key(KeyCode::Char('g'))).unwrap();
        screen.key(key(KeyCode::Char('z'))).unwrap();
        screen.key(key(KeyCode::Enter)).unwrap();
        assert!(matches!(screen.editor, Some(Editor::Groups { .. })));
        assert!(render(&mut screen, 80, 24).contains("No matching groups"));
        screen.key(key(KeyCode::Esc)).unwrap();
        assert_eq!(screen.group, ["System"]);
    }

    #[test]
    fn prerequisite_back_restores_group_and_changed_views_after_review() {
        for changed in [false, true] {
            let (mut screen, _requests, _temp) = screen();
            let mut a = symbol("Dependent", "n", "y");
            a["prerequisites"] = json!(["B"]);
            a["locations"][0]["menu"] = json!(["Debug"]);
            screen.accept(Resolved::Preview(
                1,
                preview(json!({"A":a,"B":symbol("Parent","n","y")})),
            ));
            screen.group = if changed {
                Vec::new()
            } else {
                vec!["Debug".into()]
            };
            let previous_group = screen.group.clone();
            screen.changed_only = changed;
            screen.draft.symbols.insert("A".into(), None);
            screen.key(key(KeyCode::Char('p'))).unwrap();
            screen.key(key(KeyCode::Enter)).unwrap();
            assert_eq!(screen.selected_name().as_deref(), Some("B"));
            assert!(screen.group.is_empty() && !screen.changed_only);
            screen.key(key(KeyCode::Char('r'))).unwrap();
            screen.key(key(KeyCode::Esc)).unwrap();
            assert_eq!(screen.selected_name().as_deref(), Some("B"));
            screen.key(key(KeyCode::Esc)).unwrap();
            assert_eq!(screen.selected_name().as_deref(), Some("A"));
            assert_eq!(screen.group, previous_group);
            assert_eq!(screen.changed_only, changed);
        }
    }

    #[test]
    fn changed_keeps_hidden_requests_and_save_preserves_the_selected_setting() {
        let (mut screen, _requests, _temp) = screen();
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"A":symbol("Other","y","y"),"B":symbol("Dependent","y","y")})),
        ));
        screen.draft.symbols.insert("B".into(), None);
        screen.key(key(KeyCode::Char('c'))).unwrap();
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"A":symbol("Other","y","y"),"B":symbol("Dependent","n","n")})),
        ));
        assert_eq!(screen.names(), ["B"]);
        assert_eq!(screen.selected_name().as_deref(), Some("B"));
        assert!(screen.history.is_empty());
        assert!(render(&mut screen, 80, 24).contains("* [Off unavailable]"));
        screen.pane = Pane::Review;
        let saved = screen.preview.take().unwrap();
        screen.accept(Resolved::Save {
            revision: 1,
            preview: saved,
            result: Ok(Arc::clone(&screen.session)),
            published: true,
        });
        assert!(!screen.changed_only);
        assert!(screen.draft.symbols.is_empty());
        assert!(!screen.message.contains("now unavailable"));
        screen.key(key(KeyCode::Esc)).unwrap();
        assert_eq!(screen.selected_name().as_deref(), Some("B"));
        assert!(matches!(screen.pane, Pane::Detail));
    }

    #[test]
    fn completing_resolution_keeps_an_open_review_when_a_setting_becomes_hidden() {
        let (mut screen, _requests, _temp) = screen();
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"A":symbol("Dependent","y","y"),"B":symbol("Parent","y","y")})),
        ));
        screen.edit("B".into(), false).unwrap();
        screen.key(key(KeyCode::Char('r'))).unwrap();
        screen.accept(Resolved::Preview(
            2,
            preview(json!({"A":symbol("Dependent","n","n"),"B":symbol("Parent","n","y")})),
        ));
        assert!(matches!(screen.pane, Pane::Review));
        assert_eq!(screen.selected_name().as_deref(), Some("A"));
        screen.key(key(KeyCode::Esc)).unwrap();
        assert!(matches!(screen.pane, Pane::Detail));
        assert_eq!(screen.selected_name().as_deref(), Some("A"));
    }

    #[test]
    fn changing_a_choice_in_changed_keeps_the_chosen_symbol_focused() {
        let (mut screen, requests, _temp) = screen();
        let symbols: serde_json::Map<String, Value> = ["A", "B", "C"]
            .into_iter()
            .map(|name| {
                let mut setting = symbol(name, if name == "C" { "y" } else { "n" }, "y");
                setting["choice"] =
                    json!({"name":"CHOICE", "selected":"C", "alternatives":["A","B","C"]});
                (name.to_owned(), setting)
            })
            .collect();
        screen.accept(Resolved::Preview(
            1,
            preview(Value::Object(symbols.clone())),
        ));
        screen.draft.symbols.insert("C".into(), Some("y".into()));
        screen.key(key(KeyCode::Char('c'))).unwrap();
        assert_eq!(screen.selected_name().as_deref(), Some("C"));
        screen.key(key(KeyCode::Char(' '))).unwrap();
        screen.key(key(KeyCode::Up)).unwrap(); // Select B.
        screen.key(key(KeyCode::Enter)).unwrap();
        assert!(matches!(requests.try_recv().unwrap(), Work::Preview(2, _)));
        assert_eq!(screen.names(), ["A", "B", "C"]);
        assert_eq!(screen.selected_name().as_deref(), Some("B"));
        screen.accept(Resolved::Preview(2, preview(Value::Object(symbols))));
        assert_eq!(screen.selected_name().as_deref(), Some("B"));
    }

    #[test]
    fn group_picker_keeps_deep_sibling_names_visible() {
        let (mut screen, _requests, _temp) = screen();
        let mut setting = symbol("Setting", "y", "y");
        let parent = "A long native parent menu whose name occupies most of the terminal width";
        setting["locations"] = json!([
            {"prompt":"Setting","menu":[parent,"Serial input"]},
            {"prompt":"Setting","menu":[parent,"Serial output"]}
        ]);
        screen.accept(Resolved::Preview(1, preview(json!({"A":setting}))));
        screen.key(key(KeyCode::Char('g'))).unwrap();
        for c in "serial".chars() {
            screen.key(key(KeyCode::Char(c))).unwrap();
        }
        let rendered = render(&mut screen, 80, 24);
        assert!(rendered.contains("Serial input"));
        assert!(rendered.contains("Serial output"));
        screen.key(key(KeyCode::Down)).unwrap();
        screen.key(key(KeyCode::Enter)).unwrap();
        assert_eq!(screen.group, [parent, "Serial output"]);
    }

    #[test]
    fn quitting_protects_source_changes_but_not_noops_or_published_sources() {
        let (mut screen, _requests, _temp) = screen();
        screen.accept(Resolved::Preview(
            1,
            preview(json!({"A":symbol("Setting","y","y")})),
        ));
        // An explicit source override can leave the native value unchanged.
        screen.draft.symbols.insert("A".into(), Some("y".into()));
        let path = screen.session.root.join("fixture.conf");
        screen
            .preview
            .as_mut()
            .unwrap()
            .edits
            .insert(path, "CONFIG_A=y\n".into());
        assert!(!screen.key(key(KeyCode::Char('q'))).unwrap());
        assert!(render(&mut screen, 80, 24).contains("Discard unsaved edits?"));
        assert!(screen.shortcuts().contains("Keep editing"));
        assert!(!screen.shortcuts().contains("q Quit"));
        assert!(!screen.key(key(KeyCode::Esc)).unwrap());
        assert!(screen.editor.is_none());
        screen
            .key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL))
            .unwrap();
        assert!(matches!(screen.editor, Some(Editor::Quit)));
        assert!(!screen.key(key(KeyCode::Enter)).unwrap()); // Safe default.
        screen.key(key(KeyCode::Char('q'))).unwrap();
        assert!(screen.key(key(KeyCode::Char('y'))).unwrap());
        screen.editor = None;
        screen.retry = true;
        assert!(screen.key(key(KeyCode::Char('q'))).unwrap());
        screen.retry = false;
        screen.preview.as_mut().unwrap().edits.clear();
        assert!(screen.key(key(KeyCode::Char('q'))).unwrap());
        screen.pending = true;
        assert!(!screen.key(key(KeyCode::Char('q'))).unwrap());
        assert!(matches!(screen.editor, Some(Editor::Quit)));
    }

    #[test]
    fn focus_is_visible_and_tab_does_not_open_review() {
        let (mut screen, _requests, _temp) = screen();
        let mut a = symbol("Readable setting", "y", "y");
        a["defaults"] = json!([{"value":"n","when":{"expression":"y","value":"y"}}]);
        screen.accept(Resolved::Preview(1, preview(json!({"A":a}))));
        for (width, height) in [(80, 24), (126, 34)] {
            screen.pane = Pane::List;
            assert!(render(&mut screen, width, height).contains("▶ Settings"));
            screen.key(key(KeyCode::Tab)).unwrap();
            let detail = render(&mut screen, width, height);
            assert!(detail.contains("▶ Setting "));
            assert!(detail.contains("Current value: On (y)"));
            screen.key(key(KeyCode::Tab)).unwrap();
            assert!(matches!(screen.pane, Pane::List));
        }
        screen.key(key(KeyCode::Char('t'))).unwrap();
        assert!(screen.detail().contains("Default n when y"));
        screen.key(key(KeyCode::Char('t'))).unwrap();
        assert!(!screen.detail().contains("Default n when y"));
        assert!(screen.shortcuts().contains("Review"));
        assert!(!screen.shortcuts().contains("Save & sync"));
        screen.key(key(KeyCode::Char('S'))).unwrap();
        assert!(matches!(screen.pane, Pane::Review));
        assert!(screen.shortcuts().contains("Save & sync"));
        screen.key(key(KeyCode::Char('S'))).unwrap();
        assert!(screen.saving);
        assert!(!screen.shortcuts().contains("q Quit"));
        assert!(!screen.key(key(KeyCode::Char('q'))).unwrap());
        assert!(render(&mut screen, 80, 24).contains("Saving and preparing"));
    }

    #[test]
    fn review_shows_blockers_on_the_first_screen_and_keeps_the_final_change_reachable() {
        let (mut screen, _requests, _temp) = screen();
        let before: serde_json::Map<String, Value> = (0..40)
            .map(|n| (format!("SETTING_{n:02}"), symbol("Fixture", "n", "y")))
            .collect();
        screen.accept(Resolved::Preview(1, preview(Value::Object(before))));
        let after: serde_json::Map<String, Value> = (0..40)
            .map(|n| (format!("SETTING_{n:02}"), symbol("Fixture", "y", "y")))
            .collect();
        let mut changed = preview(Value::Object(after));
        changed.report["problems"] = json!(["Choose a supported native value before saving."]);
        changed
            .authored
            .push("example/native.conf\n  CONFIG_SETTING_00: n -> y".into());
        screen.accept(Resolved::Preview(1, changed));
        screen.pane = Pane::Review;
        for (width, height) in [(80, 24), (126, 34)] {
            screen.scroll = 0;
            let first = render(&mut screen, width, height);
            assert!(first.contains("40 native changes"));
            assert!(first.contains("Choose a supported native value"));
            screen.scroll = u16::MAX;
            assert!(render(&mut screen, width, height).contains("CONFIG_SETTING_39"));
        }
    }

    #[test]
    fn headless_selection_uses_the_supported_flag() {
        let error = select(
            "application",
            "--app",
            &["first".into(), "second".into()],
            false,
        )
        .unwrap_err();
        assert!(error.starts_with("select --app ("));
        assert!(!error.contains("--application"));
    }

    #[test]
    fn detail_explains_an_unavailable_setting_at_both_terminal_sizes() {
        let setting = json!({
            "type":"bool", "effective":"n", "visibility":"n",
            "locations":[{"prompt":"Fixture","help":"Long explanatory help comes after the blocker."}],
            "requires":{"expression":"CONFIG_A || CONFIG_B","value":"n"}
        });
        let detail = describe("FIXTURE", &setting);
        assert!(detail.find("Requires").unwrap() < detail.find("Long explanatory help").unwrap());
        let (mut screen, _requests, _temp) = screen();
        screen.accept(Resolved::Preview(1, preview(json!({"FIXTURE":setting}))));
        screen.filter = "CONFIG_FIXTURE".into();
        screen.pane = Pane::Detail;
        for (width, height) in [(80, 24), (126, 34)] {
            let rendered = render(&mut screen, width, height);
            assert!(rendered.contains("native requirements are not met"));
            assert!(rendered.contains("Requires CONFIG_A || CONFIG_B (n)"));
        }
    }
}
