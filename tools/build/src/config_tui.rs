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
        result: Result<()>,
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
    saved: bool,
    preview: Option<Preview>,
    baseline: Option<Value>,
    filter: String,
    selected: usize,
    pane: Pane,
    editor: Option<Editor>,
    input: String,
    message: String,
    scroll: u16,
    history: Vec<(String, usize)>,
}

pub fn select(kind: &str, choices: &[String], interactive: bool) -> Result<String> {
    if let [choice] = choices {
        return Ok(choice.clone());
    }
    if !interactive || !io::stdin().is_terminal() || !io::stderr().is_terminal() {
        return Err(format!("select --{kind} ({})", choices.join(", ")));
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
    let worker_session = Arc::clone(&session);
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
                            Ok(true) => (
                                worker_session
                                    .sync(&preview, &mut inspection)
                                    .and_then(|_| sync_editor(&worker_session)),
                                true,
                            ),
                            Ok(false) => (Ok(()), false),
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
            saved: false,
            preview: None,
            baseline: None,
            filter: String::new(),
            selected: 0,
            pane: Pane::List,
            editor: None,
            input: String::new(),
            message: String::new(),
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
                while let Ok(done) = receiver.try_recv() {
                    screen.accept(done);
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
        self.sender
            .send(Work::Preview(self.revision, self.draft.clone()))
            .map_err(|e| e.to_string())
    }

    fn accept(&mut self, done: Resolved) {
        match done {
            Resolved::Preview(revision, preview) if revision == self.revision => {
                self.pending = false;
                if self.baseline.is_none() && preview.report["symbols"].is_object() {
                    self.baseline = Some(preview.report.clone());
                }
                self.preview = Some(preview);
                self.selected = self.selected.min(self.names().len().saturating_sub(1));
            }
            Resolved::Save {
                revision,
                preview,
                result,
                published,
            } if revision == self.revision => {
                self.saving = false;
                match result {
                    Ok(()) => {
                        self.retry = false;
                        self.saved = published;
                        self.message = if published {
                            "Synced. Reopen to make further edits."
                        } else {
                            "No source changes."
                        }
                        .into();
                    }
                    Err(error) if published => {
                        self.retry = true;
                        self.message = format!("Saved; sync failed. Press S to retry. {error}");
                    }
                    Err(error) => self.message = error,
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
            .or_else(|| self.baseline.as_ref().map(|value| &value["symbols"]))
            .unwrap_or(&Value::Null)
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
        if self.pending || self.saving || self.saved {
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
        if self.saving || self.saved || self.retry || self.baseline.is_none() {
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
                            self.history.push((self.filter.clone(), self.selected));
                            self.filter = format!("CONFIG_{name}");
                            self.selected = 0;
                            self.pane = Pane::Detail;
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
                if let Some((filter, selected)) = self.history.pop() {
                    self.filter = filter;
                    self.selected = selected;
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
                if !self.saved && !self.retry =>
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
                        KeyCode::Char('d') if !self.pending => {
                            if self.session.pin_owner(&name).is_ok() {
                                self.draft.pins.remove(&name);
                            } else {
                                self.draft.symbols.insert(name, None);
                            }
                            self.resolve()?;
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

    fn render(&self, frame: &mut Frame) {
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
            "Resolving draft"
        } else if self.retry {
            "Saved; sync failed"
        } else if self.saved {
            "Synced"
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
        if matches!(self.pane, Pane::Review) {
            self.paragraph(frame, body, "Review", self.review());
        } else if frame.area().width >= 105 {
            let columns = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Percentage(43), Constraint::Percentage(57)])
                .split(body);
            self.list(frame, columns[0]);
            self.paragraph(frame, columns[1], "Setting", self.detail());
        } else if matches!(self.pane, Pane::List) {
            self.list(frame, body);
        } else {
            self.paragraph(frame, body, "Setting", self.detail());
        }
        let status = self.message.lines().next().unwrap_or("");
        frame.render_widget(Paragraph::new(format!("/ Search  ↑↓ Move  Enter Detail  Space Edit  p Prerequisites\nTab Pane  d Reset  r Review  S Save & sync  Esc Back  q Exit\n{status}")), areas[2]);
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
                            format!(
                                "{} {}  {}",
                                if index == *selected { ">" } else { " " },
                                name,
                                prompt(&self.catalog()[name])
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
                    "{marker} [{effective}{disposition}] {name}  {label}",
                    effective = value_string(&symbol["effective"])
                ));
                if index == self.selected {
                    row.style(Style::default().add_modifier(Modifier::REVERSED))
                } else {
                    row
                }
            })
            .collect::<Vec<_>>();
        let title = if self.filter.is_empty() {
            format!("Settings ({})", names.len())
        } else {
            format!("Search {} ({})", self.filter, names.len())
        };
        frame.render_widget(
            List::new(rows).block(Block::default().title(title).borders(Borders::ALL)),
            area,
        );
    }

    fn paragraph(&self, frame: &mut Frame, area: Rect, title: &str, body: String) {
        let scroll = self
            .scroll
            .min(body.lines().count().saturating_sub(1) as u16);
        frame.render_widget(
            Paragraph::new(body)
                .wrap(Wrap { trim: false })
                .scroll((scroll, 0))
                .block(Block::default().title(title).borders(Borders::ALL)),
            area,
        );
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
        if count == 0 {
            rows.push("  None".into());
        }
        rows.push("\nProblems to resolve".into());
        let problems = preview.report["problems"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        if problems.is_empty() {
            rows.push("  None".into());
        }
        rows.extend(
            problems
                .iter()
                .filter_map(Value::as_str)
                .map(|problem| format!("  {problem}")),
        );
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
            if let Some(help) = node["help"].as_str() {
                lines.push(help.into());
            }
            if let Some(prompt_condition) = node["prompt_condition"]["expression"].as_str() {
                if node["prompt_condition"] != symbol["requires"] {
                    lines.push(format!(
                        "Prompt visible when {prompt_condition} ({})",
                        value_string(&node["prompt_condition"]["value"])
                    ));
                }
            }
        }
    }
    let requires = &symbol["requires"];
    lines.push(format!(
        "Requires {} ({})",
        value_string(&requires["expression"]),
        value_string(&requires["value"])
    ));
    for (field, label) in [("enabled_by", "Enabled by"), ("implied_by", "Suggested by")] {
        if let Some(entries) = symbol[field].as_array() {
            let mut inactive = 0;
            for entry in entries {
                if entry["when"]["value"] == "n" {
                    inactive += 1;
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
            if inactive != 0 {
                lines.push(format!("{label} {inactive} conditional entries inactive"));
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
    if let Some(choice) = symbol["choice"].as_object() {
        let choice = &Value::Object(choice.clone());
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
    lines.join("\n")
}
