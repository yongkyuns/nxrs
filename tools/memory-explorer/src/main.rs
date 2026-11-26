//! Native viewer for linked firmware and independently captured memory snapshots.

mod ui;

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

const HELP: &str = "NXRS Memory Explorer\n\n\
Usage: nxrs-memory-explorer [--elf FINAL_ELF] [--map FINAL_MAP]\n\
                            [--snapshot CAPTURE.json]... [--layout LAYOUT.json]\n\
                            [--trace TRACE.json] [--view overview|memory|timing]\n\
       nxrs-memory-explorer --demo\n\n\
An empty invocation opens the file-loading screen. --demo opens synthetic data.\n\
Snapshots describe captured points only; the viewer does not infer intervening events.\n\n\
Options:\n\
  --elf PATH          Exact final ELF used by the captured firmware\n\
  --map PATH          Linker map from the same build\n\
  --snapshot PATH     Snapshot JSON; may be repeated\n\
  --layout PATH       Declared region and physical-backing layout JSON\n\
  --trace PATH        Chrome Trace Event JSON (also opens without an ELF)\n\
  --view NAME         Initial view: overview, memory, or timing\n\
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
}

impl Arguments {
    fn parse() -> Result<Option<Self>, String> {
        let mut args = std::env::args().skip(1);
        let mut parsed = Self::default();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--elf" => parsed.inputs.elf = required_value(&mut args, &arg)?,
                "--map" => parsed.inputs.map = required_value(&mut args, &arg)?,
                "--layout" => parsed.inputs.layout = required_value(&mut args, &arg)?,
                "--trace" => parsed.inputs.trace = required_value(&mut args, &arg)?,
                "--view" => {
                    parsed.view = Some(ui::ViewMode::parse(&required_value(&mut args, &arg)?)?)
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
        if parsed.demo && parsed.inputs.has_inputs() {
            return Err("--demo cannot be combined with firmware input files".into());
        }
        if parsed.smoke_test
            && !parsed.demo
            && parsed.inputs.elf.is_empty()
            && parsed.inputs.trace.is_empty()
        {
            return Err("--smoke-test requires --demo, --elf, or --trace".into());
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
        return Ok(());
    };
    let result = Arc::new(Mutex::new(None));
    let smoke = args.smoke_test.then(|| ui::SmokeOptions {
        screenshot: args
            .screenshot
            .unwrap_or_else(|| "/tmp/nxrs-memory-smoke.ppm".into()),
        result: Arc::clone(&result),
    });
    let mut app = ui::MemoryExplorer::new(args.inputs, args.demo, smoke)?;
    if let Some(view) = args.view {
        app.set_view(view)?;
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
