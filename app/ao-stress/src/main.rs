//! Measure this execution platform; do not infer MCU timing from host results.
#![forbid(unsafe_code)]

#[cfg(all(test, feature = "probe"))]
#[allow(dead_code)] // Executed by the dedicated harness-free qualification target.
mod memory_qualification;
pub(crate) mod stress;
use std::process::ExitCode;
use std::time::Duration;
use stress::{Config, Scenario};

#[cfg(all(test, feature = "probe"))]
#[allow(dead_code)] // Entry used by the dedicated harness-free test executable.
pub(crate) fn qualify_memory() {
    memory_qualification::run();
}

type Observer = fn(&Config, Scenario, usize) -> Result<(), String>;

pub(crate) fn run(observer: Option<Observer>) -> Result<(), String> {
    let mut config = Config::default();
    let mut scenario = "all".to_owned();
    let mut rounds = 1usize;
    let mut stack_report = false;
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        if flag == "--help" {
            if args.next().is_some() {
                return Err("--help takes no other arguments".into());
            }
            println!("usage: ao-stress [--scenario all|steady|burst|slow-consumer|cpu-load]\n  [--duration-ms 10..60000] [--rounds 1..100] [--producers 1..8]\n  [--workers 1..8] [--capacity 1..256] [--work 0..100000]\n  [--deadline-us 1..10000000] [--shutdown-ms 10..10000] [--stack-report]");
            return Ok(());
        }
        if flag == "--stack-report" {
            stack_report = true;
            continue;
        }
        let value = args
            .next()
            .ok_or_else(|| format!("missing value for {flag}"))?;
        if flag == "--scenario" {
            scenario = value;
            continue;
        }
        let value = value
            .parse::<u64>()
            .map_err(|_| format!("invalid integer for {flag}"))?;
        match flag.as_str() {
            "--duration-ms" => config.duration = Duration::from_millis(value),
            "--rounds" => rounds = usize::try_from(value).map_err(|_| "rounds too large")?,
            "--producers" => {
                config.producers = usize::try_from(value).map_err(|_| "producers too large")?
            }
            "--workers" => {
                config.workers = usize::try_from(value).map_err(|_| "workers too large")?
            }
            "--capacity" => {
                config.capacity = usize::try_from(value).map_err(|_| "capacity too large")?
            }
            "--work" => config.work = u32::try_from(value).map_err(|_| "work too large")?,
            "--deadline-us" => config.deadline = Duration::from_micros(value),
            "--shutdown-ms" => config.shutdown = Duration::from_millis(value),
            _ => return Err(format!("unknown option {flag}")),
        }
    }
    config.validate()?;
    if stack_report && observer.is_none() {
        return Err("--stack-report requires the stack qualification executable".into());
    }
    if !(1..=100).contains(&rounds) {
        return Err("rounds must be in 1..=100".into());
    }
    let scenarios: &[Scenario] = match scenario.as_str() {
        "all" => &Scenario::ALL,
        "steady" => &[Scenario::Steady],
        "burst" => &[Scenario::Burst],
        "slow-consumer" => &[Scenario::SlowConsumer],
        "cpu-load" => &[Scenario::CpuLoad],
        _ => return Err("unknown scenario; use --help".into()),
    };
    stress::transport_edges()?;
    for round in 1..=rounds {
        for &scenario in scenarios {
            let report = if stack_report {
                let observe = observer.ok_or("missing stack observer")?;
                let mut observer = || observe(&config, scenario, round);
                stress::run_observed(&config, scenario, &mut observer)?
            } else {
                stress::run(&config, scenario)?
            };
            report.print(&config, scenario, round);
        }
    }
    println!(
        "AO_STRESS PASS scenarios={} rounds={rounds}",
        scenarios.len()
    );
    Ok(())
}

fn main() -> ExitCode {
    match run(None) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("AO_STRESS FAIL: {error}");
            ExitCode::FAILURE
        }
    }
}
