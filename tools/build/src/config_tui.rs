//! Keyboard-driven native configuration.
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
    style::{Modifier, Style},
    text::Line,
    widgets::{Block, Borders, List, Paragraph, Wrap},
    Frame, Terminal,
};
use serde_json::Value;
use std::{
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

enum Pane {
    List,
    Detail,
    Review,
}

enum Editor {
    Search,
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
    selected: usize,
    pane: Pane,
    editor: Option<Editor>,
    input: String,
    message: String,
    save_error: Option<String>,
    scroll: u16,
    history: Vec<(String, String)>,
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
                self.saving = false;
                match result {
                    Ok(session) => {
                        self.session = session;
                        self.retry = false;
                        self.save_error = None;
                        self.draft = Draft::default();
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
            self.history.push((self.filter.clone(), name.to_owned()));
            self.filter = format!("CONFIG_{name}");
            self.selected = 0;
            self.pane = Pane::Detail;
            self.scroll = 0;
            self.message = "This setting is now unavailable. Escape returns to the list.".into();
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
        if !exact.is_empty() {
            if let Some((name, _)) = symbols.get_key_value(&exact.to_uppercase()) {
                return vec![name.as_str()];
            }
        }
        symbols
            .iter()
            .filter(|(name, value)| {
                query.is_empty() && value["visibility"] != "n"
                    || !query.is_empty()
                        && (name.to_lowercase().contains(exact)
                            || prompt(value).to_lowercase().contains(&query))
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
            Editor::Choose {
                options,
                mut selected,
                prerequisite,
            } => {
                if key.code == KeyCode::Enter {
                    if let Some(name) = options.get(selected) {
                        if prerequisite {
                            if let Some(current) = self.selected_name() {
                                self.history.push((self.filter.clone(), current));
                            }
                            self.filter = format!("CONFIG_{name}");
                            self.selected = 0;
                            self.pane = Pane::Detail;
                            self.scroll = 0;
                        } else {
                            if self.catalog()[name]["choice"]["selected"] == name.as_str() {
                                return Ok(());
                            }
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
                                self.filter = self.input.clone();
                                self.selected = 0;
                                self.pane = Pane::List;
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
        if let Some(editor) = self.editor.take() {
            self.edited_input(editor, key)?;
            return Ok(false);
        }
        if self.saving {
            return Ok(false);
        }
        match key.code {
            KeyCode::Char('q') => return Ok(true),
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => return Ok(true),
            KeyCode::Esc => {
                if matches!(self.pane, Pane::Review) {
                    self.pane = Pane::Detail;
                } else if let Some((filter, selected)) = self.history.pop() {
                    self.filter = filter;
                    self.selected = self
                        .names()
                        .iter()
                        .position(|name| *name == selected)
                        .unwrap_or(0);
                    if !self.names().iter().any(|name| *name == selected) {
                        self.pane = Pane::List;
                    }
                } else if !matches!(self.pane, Pane::List) {
                    self.pane = Pane::List;
                } else {
                    return Ok(true);
                }
                self.scroll = 0;
            }
            KeyCode::Char('/') => {
                self.input.clear();
                self.editor = Some(Editor::Search);
            }
            KeyCode::Char('S') => self.save()?,
            KeyCode::Char('r') => {
                self.pane = Pane::Review;
                self.scroll = 0;
            }
            KeyCode::Tab => {
                self.pane = match self.pane {
                    Pane::List => Pane::Detail,
                    Pane::Detail => Pane::Review,
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
                if !self.retry && !matches!(self.pane, Pane::Review) =>
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
                            if self.pending {
                                self.message =
                                    "Wait for the current edit to resolve before resetting.".into();
                            } else {
                                if self.session.pin_owner(&name).is_ok() {
                                    self.draft.pins.remove(&name);
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
            "Draft"
        };
        frame.render_widget(
            Paragraph::new(format!(
                "{} • {} • {board}\n{status}",
                self.session.app.bin,
                self.session.profile.name()
            )),
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
            self.paragraph(frame, body, "Configuration unavailable", problems);
        } else if matches!(self.pane, Pane::Review) {
            self.paragraph(frame, body, "Review", self.review());
        } else if frame.area().width >= 105 {
            let columns = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Percentage(43), Constraint::Percentage(57)])
                .split(body);
            self.list(frame, columns[0]);
            self.paragraph(frame, columns[1], detail_title, self.detail());
        } else if matches!(self.pane, Pane::List) {
            self.list(frame, body);
        } else {
            self.paragraph(frame, body, detail_title, self.detail());
        }
        let shortcuts = match self.pane {
            Pane::List => "/ Search  ↑↓ Move  Enter Detail  Space Edit  p Prerequisites\nTab Pane  d Reset  r Review  S Save & sync  Esc Back  q Exit",
            Pane::Detail => "↑↓ Scroll  Enter/Space Edit  p Prerequisites  / Search\nTab Pane  d Reset  r Review  S Save & sync  Esc Back  q Exit",
            Pane::Review if self.retry => "↑↓ Scroll  S Retry sync\nTab Settings  Esc Back  q Exit",
            Pane::Review => "↑↓ Scroll  S Save & sync\nTab Settings  Esc Back  q Exit",
        };
        let status = self
            .message
            .lines()
            .next()
            .filter(|line| !line.is_empty())
            .or_else(|| self.problems().iter().find_map(Value::as_str))
            .unwrap_or("");
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
                Editor::Search => (
                    "Search",
                    format!("{}▏\nEnter Search · Esc Cancel", self.input),
                ),
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
                        .skip(selected.saturating_sub(4))
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
                Paragraph::new(contents).block(Block::default().title(title).borders(Borders::ALL)),
                popup,
            );
        }
    }

    fn list(&self, frame: &mut Frame, area: Rect) {
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
                let disposition = if symbol.get("ownership").is_some() {
                    " owned"
                } else if symbol["visibility"] == "n" {
                    " unavailable"
                } else {
                    ""
                };
                let label = prompt(symbol);
                let row = Line::raw(format!(
                    "{marker} [{effective}{disposition}] {label} · CONFIG_{name}",
                    effective = value_string(&symbol["effective"])
                ));
                if index == self.selected {
                    row.style(Style::default().add_modifier(Modifier::REVERSED))
                } else {
                    row
                }
            })
            .collect::<Vec<_>>();
        let mut title = if self.filter.is_empty() {
            format!("Settings ({})", names.len())
        } else {
            format!("Search {} ({})", self.filter, names.len())
        };
        if self.stale_catalog() {
            title.push_str(" · last resolved");
        }
        frame.render_widget(
            List::new(rows).block(Block::default().title(title).borders(Borders::ALL)),
            area,
        );
    }

    fn paragraph(&mut self, frame: &mut Frame, area: Rect, title: &str, body: String) {
        let block = Block::default().title(title).borders(Borders::ALL);
        let inner = block.inner(area);
        let paragraph = Paragraph::new(body).wrap(Wrap { trim: false });
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
        describe(name, &self.catalog()[name])
    }

    fn review(&self) -> String {
        let Some(preview) = &self.preview else {
            return "Resolving the draft…".into();
        };
        let mut rows = vec!["Your edits".to_owned()];
        if preview.authored.is_empty() {
            rows.push("  None".into());
        }
        rows.extend(preview.authored.iter().map(|line| format!("  {line}")));
        rows.push("\nResulting native changes".into());
        let mut count = 0;
        if let (Some(before), Some(after)) = (&self.baseline, preview.report["symbols"].as_object())
        {
            for (name, symbol) in after {
                let old = &before["symbols"][name]["effective"];
                let new = &symbol["effective"];
                if old != new {
                    count += 1;
                    rows.push(format!(
                        "  CONFIG_{name}: {} → {}",
                        value_string(old),
                        value_string(new)
                    ));
                }
            }
        }
        if !preview.report["symbols"].is_object() {
            rows.push("  Unavailable until this draft resolves.".into());
        } else if count == 0 {
            rows.push("  None".into());
        }
        rows.push("\nProblems to resolve".into());
        let problems = preview.report["problems"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        if problems.is_empty() && self.save_error.is_none() {
            rows.push("  None".into());
        }
        rows.extend(
            problems
                .iter()
                .filter_map(Value::as_str)
                .map(|problem| format!("  {problem}")),
        );
        if let Some(error) = &self.save_error {
            rows.push(format!("  {error}"));
        }
        rows.join("\n")
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
            assert!(rendered.contains("no visible prompt"));
            assert!(rendered.contains("Requires CONFIG_A || CONFIG_B (n)"));
        }
    }
}
