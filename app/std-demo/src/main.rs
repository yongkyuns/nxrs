//! Small executable recipes, not a benchmark or an allocation-free std claim.
#![forbid(unsafe_code)]

mod collections;
mod concurrency;

use std::process::ExitCode;

type DemoResult<T = ()> = Result<T, Box<dyn std::error::Error + Send + Sync>>;
type Recipe = (&'static str, fn() -> DemoResult);

#[cfg(all(test, feature = "probe"))]
#[allow(dead_code)] // Entry used by the dedicated harness-free test executable.
pub(crate) fn qualify_memory() {
    collections::memory_qualification::run();
}

const RECIPES: &[Recipe] = &[
    ("vec", collections::vectors),
    ("fixed", collections::fixed_storage),
    ("maps", collections::maps),
    ("queues", collections::queues),
    ("bytes", collections::bytes_and_text),
    ("ownership", concurrency::ownership),
    ("channels", concurrency::channels),
    ("synchronization", concurrency::synchronization),
    ("deadlines", concurrency::deadlines),
];

fn run() -> DemoResult {
    let mut args = std::env::args().skip(1);
    let selected = match args.next().as_deref() {
        None => "all".to_owned(),
        Some("--case") => args.next().ok_or("missing --case value")?,
        Some("--list") if args.next().is_none() => {
            for (name, _) in RECIPES {
                println!("{name}");
            }
            return Ok(());
        }
        Some("--help") if args.next().is_none() => {
            println!("usage: std-demo [--case all|<name> | --list]");
            return Ok(());
        }
        _ => return Err("usage: std-demo [--case all|<name> | --list]".into()),
    };
    if args.next().is_some()
        || (selected != "all" && !RECIPES.iter().any(|(name, _)| *name == selected))
    {
        return Err("unknown case or extra argument; use --list".into());
    }
    let mut completed = 0;
    for (name, recipe) in RECIPES {
        if selected == "all" || *name == selected {
            println!("STD_DEMO case={name} status=running");
            recipe()?;
            println!("STD_DEMO case={name} status=pass");
            completed += 1;
        }
    }
    println!("STD_DEMO PASS cases={completed}");
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("STD_DEMO FAIL: {error}");
            ExitCode::FAILURE
        }
    }
}
