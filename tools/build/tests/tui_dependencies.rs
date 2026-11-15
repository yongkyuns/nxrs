//! Compatibility guards for the native configuration editor's terminal stack.
use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    backend::TestBackend,
    buffer::Buffer,
    layout::{Constraint, Direction, Layout},
    widgets::{Block, Borders, Paragraph, Wrap},
    Terminal,
};

#[test]
fn input_and_rendering_share_the_same_crossterm_types() {
    // Different crossterm versions have different event types and terminal state.
    // This assignment must stop compiling if the direct dependency and backend diverge.
    let key = KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL);
    let backend_event: ratatui::crossterm::event::Event = Event::Key(key);
    assert_eq!(backend_event, ratatui::crossterm::event::Event::Key(key));
}

fn render(width: u16, height: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| {
            let panes = Layout::default()
                .direction(Direction::Vertical)
                .constraints([
                    Constraint::Length(3),
                    Constraint::Min(1),
                    Constraint::Length(2),
                ])
                .split(frame.area());
            frame.render_widget(
                Paragraph::new("Native configuration")
                    .block(Block::default().borders(Borders::ALL)),
                panes[0],
            );
            frame.render_widget(
                Paragraph::new("A long setting description wraps without changing the saved draft.")
                    .wrap(Wrap { trim: false }),
                panes[1],
            );
            frame.render_widget(Paragraph::new("Review / Save & sync"), panes[2]);
        })
        .unwrap();
    terminal.backend().buffer().clone()
}

#[test]
fn rendering_is_stable_across_terminal_size_sequences() {
    // Exercise the layout cache and text wrapping across small and large terminals.
    let baseline = render(80, 24);
    let wide_baseline = render(126, 34);
    for width in 32..160 {
        let _ = render(width, 16 + width % 20);
    }
    assert_eq!(render(80, 24), baseline);
    assert_eq!(render(126, 34), wide_baseline);
}
