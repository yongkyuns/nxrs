//! Native viewer for linked firmware and independently captured memory snapshots.

mod ui;

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

const HELP: &str = "NXRS Memory Explorer\n\n\
Usage: nxrs-memory-explorer [--elf FINAL_ELF] [--map FINAL_MAP]\n\
                            [--snapshot CAPTURE.json]... [--layout LAYOUT.json]\n\
                            [--trace TRACE.json] [--heap-journal JOURNAL.json]\n\
                            [--view overview|memory|timing|timing-events|timing-sources]\n\
       nxrs-memory-explorer --session SESSION.json [--session OTHER.json]... [--view NAME]\n\
       nxrs-memory-explorer --demo\n\n\
An empty invocation opens the file-loading screen. --demo opens synthetic data.\n\
Snapshots describe captured points only. Heap replay requires a separate validated journal.\n\n\
Options:\n\
  --session PATH      Open a verified capture in its own tab; may be repeated\n\
  --elf PATH          Exact final ELF used by the captured firmware\n\
  --map PATH          Linker map from the same build\n\
  --snapshot PATH     Snapshot JSON; may be repeated\n\
  --layout PATH       Declared region and physical-backing layout JSON\n\
  --board ID          Select a bundled board layout; default automatic (see --help)\n\
  --trace PATH        Chrome Trace Event JSON (also opens without an ELF)\n\
  --heap-journal PATH Heap topology journal for one explicitly identified heap\n\
  --view NAME         Initial view: overview, memory, timing, timing-events, timing-sources\n\
  --demo              Inspect an explicitly synthetic demonstration\n\
  --smoke-test        Exercise navigation, render a screenshot, and exit\n\
  --screenshot PATH   PPM output for --smoke-test (default /tmp/nxrs-memory-smoke.ppm)\n\
  -h, --help          Show this help\n";

#[derive(Default)]
struct Arguments {
    inputs: ui::InputPaths,
    demo: bool,
    smoke_test: bool,
    screenshot: Option<PathBuf>,
    view: Option<ui::ViewMode>,
    timing_analysis: Option<String>,
    sessions: Vec<PathBuf>,
}

impl Arguments {
    fn parse() -> Result<Option<Self>, String> {
        Self::parse_from(std::env::args().skip(1))
    }

    fn parse_from(args: impl IntoIterator<Item = String>) -> Result<Option<Self>, String> {
        let mut args = args.into_iter();
        let mut parsed = Self::default();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--session" => parsed
                    .sessions
                    .push(required_value(&mut args, &arg)?.into()),
                "--elf" => parsed.inputs.elf = required_value(&mut args, &arg)?,
                "--map" => parsed.inputs.map = required_value(&mut args, &arg)?,
                "--layout" => parsed.inputs.layout = required_value(&mut args, &arg)?,
                "--board" => parsed.inputs.board = required_value(&mut args, &arg)?,
                "--trace" => parsed.inputs.trace = required_value(&mut args, &arg)?,
                "--heap-journal" => parsed.inputs.heap_journal = required_value(&mut args, &arg)?,
                "--view" => {
                    let view = required_value(&mut args, &arg)?;
                    parsed.timing_analysis = match view.as_str() {
                        "timing-events" => Some("events".into()),
                        "timing-sources" => Some("sources".into()),
                        _ => None,
                    };
                    parsed.view = Some(ui::ViewMode::parse(if parsed.timing_analysis.is_some() {
                        "timing"
                    } else {
                        &view
                    })?);
                }
                "--snapshot" => parsed
                    .inputs
                    .snapshots
                    .push(required_value(&mut args, &arg)?),
                "--demo" => parsed.demo = true,
                "--smoke-test" => parsed.smoke_test = true,
                "--screenshot" => {
                    parsed.screenshot = Some(required_value(&mut args, &arg)?.into());
                }
                "--help" | "-h" => return Ok(None),
                _ => return Err(format!("Unknown argument: {arg}\n\n{HELP}")),
            }
        }
        if parsed.demo && (parsed.inputs.has_inputs() || !parsed.sessions.is_empty()) {
            return Err("--demo cannot be combined with firmware input files".into());
        }
        if !parsed.sessions.is_empty() && parsed.inputs.has_inputs() {
            return Err("--session cannot be combined with individual input files; open separate sessions in tabs, or prepare matching artifacts as one session.".into());
        }
        if !parsed.inputs.board.is_empty() {
            if !nxrs_memory_explorer::SUPPORTED_LAYOUT_BOARDS
                .iter()
                .any(|(id, _)| *id == parsed.inputs.board)
            {
                return Err(format!("Unknown board layout: {}", parsed.inputs.board));
            }
            if !parsed.inputs.layout.is_empty() {
                return Err("Choose either --board or --layout; an explicit layout path overrides automatic discovery.".into());
            }
        }
        if parsed.smoke_test
            && !parsed.demo
            && parsed.inputs.elf.is_empty()
            && parsed.inputs.trace.is_empty()
            && parsed.sessions.is_empty()
        {
            return Err("--smoke-test requires --demo, --session, --elf, or --trace".into());
        }
        if parsed.screenshot.is_some() && !parsed.smoke_test {
            return Err("--screenshot is used with --smoke-test".into());
        }
        Ok(Some(parsed))
    }
}

fn required_value(args: &mut impl Iterator<Item = String>, flag: &str) -> Result<String, String> {
    args.next()
        .filter(|value| !value.starts_with("--"))
        .ok_or_else(|| format!("{flag} requires a path"))
}

fn run() -> Result<(), String> {
    let Some(args) = Arguments::parse()? else {
        print!("{HELP}");
        println!("\nBundled board layouts:");
        for (id, label) in nxrs_memory_explorer::SUPPORTED_LAYOUT_BOARDS {
            println!("  {id:<24} {label}");
        }
        return Ok(());
    };
    let result = Arc::new(Mutex::new(None));
    let smoke = args.smoke_test.then(|| ui::SmokeOptions {
        screenshot: args
            .screenshot
            .unwrap_or_else(|| "/tmp/nxrs-memory-smoke.ppm".into()),
        result: Arc::clone(&result),
    });
    let mut app = ui::RecordingWorkspace::new(args.inputs, args.demo, args.sessions, smoke)?;
    if let Some(view) = args.view {
        app.set_view(view)?;
    }
    if let Some(mode) = args.timing_analysis {
        app.set_timing_analysis(&mode);
    }
    let native_options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_inner_size([1440.0, 940.0])
            .with_min_inner_size([1000.0, 700.0])
            .with_app_id("org.nxrs.memory-explorer"),
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eframe::run_native(
        "NXRS · Memory Explorer",
        native_options,
        Box::new(move |cc| {
            ui::configure_style(&cc.egui_ctx);
            Ok(Box::new(app))
        }),
    )
    .map_err(|error| error.to_string())?;

    if args.smoke_test {
        result
            .lock()
            .map_err(|_| "Smoke-test result lock was poisoned".to_owned())?
            .take()
            .ok_or_else(|| "The window closed before smoke validation completed".to_owned())??;
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("nxrs-memory-explorer: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Option<Arguments>, String> {
        Arguments::parse_from(args.iter().map(|arg| (*arg).to_owned()))
    }

    #[test]
    fn repeated_sessions_keep_order_and_defer_loading() {
        let args = parse(&[
            "--session",
            "first/session.json",
            "--session",
            "second",
            "--smoke-test",
        ])
        .unwrap()
        .unwrap();
        assert_eq!(
            args.sessions,
            vec![PathBuf::from("first/session.json"), PathBuf::from("second")]
        );
        assert!(!args.inputs.has_inputs());
    }

    #[test]
    fn session_flags_do_not_attach_raw_files_to_an_ambiguous_recording() {
        assert!(parse(&["--session", "one", "--elf", "nuttx"]).is_err());
        assert!(parse(&["--session", "one", "--board", "mps2-an521-qemu"]).is_err());
        assert!(parse(&["--demo", "--session", "one"]).is_err());
        assert!(parse(&["--session"]).is_err());
        assert!(parse(&["--session", "--demo"]).is_err());
    }

    #[test]
    fn explicit_board_is_supported_and_exclusive_with_layout_path() {
        assert!(parse(&["--elf", "nuttx", "--board", "mps2-an521-qemu"]).is_ok());
        assert!(parse(&["--elf", "nuttx", "--board", "unknown"]).is_err());
        assert!(parse(&[
            "--elf",
            "nuttx",
            "--board",
            "mps2-an521-qemu",
            "--layout",
            "layout.json"
        ])
        .is_err());
    }
}
