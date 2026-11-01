//! NuttX-only procfs stack high-water qualification.
//!
//! This observes owners only after their workload has completed and while the
//! completion barrier keeps every owner thread alive. NuttX stack coloration
//! retains the lifetime high-water mark in /proc/<pid>/stack.

use crate::application::stress::{Config, Scenario};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

#[derive(Debug)]
struct StackRow {
    pid: u32,
    size: usize,
    used: usize,
}

fn field<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    text.lines()
        .find_map(|line| line.strip_prefix(key).map(str::trim))
}

fn numeric_pid(path: &Path) -> Option<u32> {
    path.file_name()?.to_str()?.parse().ok()
}

fn expected_names(config: &Config) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    names.insert("ao-collector".to_owned());
    for id in 0..config.workers {
        names.insert(format!("ao-worker-{id}"));
    }
    for id in 0..config.producers {
        names.insert(format!("ao-source-{id}"));
    }
    names
}

pub(crate) fn report_stacks(
    config: &Config,
    scenario: Scenario,
    round: usize,
) -> Result<(), String> {
    let expected = expected_names(config);
    let mut owners = BTreeMap::<String, StackRow>::new();
    let mut main = None;

    for entry in fs::read_dir("/proc").map_err(|error| format!("read /proc: {error}"))? {
        let entry = entry.map_err(|error| format!("read /proc entry: {error}"))?;
        let Some(pid) = numeric_pid(&entry.path()) else {
            continue;
        };

        let status_path = entry.path().join("status");
        let Ok(status) = fs::read_to_string(&status_path) else {
            continue;
        };
        let Some(name) = field(&status, "Name:") else {
            continue;
        };
        if !expected.contains(name) && name != "ao_stress" {
            continue;
        }

        let stack_path = entry.path().join("stack");
        let stack = fs::read_to_string(&stack_path)
            .map_err(|error| format!("read {}: {error}", stack_path.display()))?;
        let size = field(&stack, "StackSize:")
            .ok_or_else(|| format!("{} missing StackSize", stack_path.display()))?
            .parse::<usize>()
            .map_err(|error| format!("{} invalid StackSize: {error}", stack_path.display()))?;
        let used = field(&stack, "StackUsed:")
            .ok_or_else(|| {
                format!(
                    "{} missing StackUsed; CONFIG_STACK_COLORATION is required",
                    stack_path.display()
                )
            })?
            .parse::<usize>()
            .map_err(|error| format!("{} invalid StackUsed: {error}", stack_path.display()))?;

        if size == 0 || used == 0 || used > size {
            return Err(format!(
                "invalid stack high-water for {name}: size={size} used={used}"
            ));
        }

        let row = StackRow { pid, size, used };
        if name == "ao_stress" {
            if main.replace(row).is_some() {
                return Err("duplicate ao_stress task in procfs".into());
            }
        } else if owners.insert(name.to_owned(), row).is_some() {
            return Err(format!("duplicate owner name in procfs: {name}"));
        }
    }

    let actual: BTreeSet<_> = owners.keys().cloned().collect();
    if actual != expected {
        let missing: Vec<_> = expected.difference(&actual).cloned().collect();
        let unexpected: Vec<_> = actual.difference(&expected).cloned().collect();
        return Err(format!(
            "owner procfs inventory mismatch: missing={missing:?} unexpected={unexpected:?}"
        ));
    }

    let mut max_used = 0usize;
    let mut min_headroom = usize::MAX;
    let mut max_fill_permille = 0usize;
    for (name, row) in &owners {
        let headroom = row.size - row.used;
        let fill_permille = row.used.saturating_mul(1000) / row.size;
        max_used = max_used.max(row.used);
        min_headroom = min_headroom.min(headroom);
        max_fill_permille = max_fill_permille.max(fill_permille);
        println!(
            "NUTTX_STACK_RESULT {{\"scenario\":\"{}\",\"round\":{},\"name\":\"{}\",\"pid\":{},\"stack_size\":{},\"stack_used\":{},\"headroom\":{},\"fill_permille\":{}}}",
            scenario.name(),
            round,
            name,
            row.pid,
            row.size,
            row.used,
            headroom,
            fill_permille
        );
    }

    if let Some(row) = main {
        let headroom = row.size - row.used;
        println!(
            "NUTTX_STACK_MAIN {{\"scenario\":\"{}\",\"round\":{},\"pid\":{},\"stack_size\":{},\"stack_used\":{},\"headroom\":{}}}",
            scenario.name(),
            round,
            row.pid,
            row.size,
            row.used,
            headroom
        );
    }

    println!(
        "NUTTX_STACK_SUMMARY {{\"scenario\":\"{}\",\"round\":{},\"owners\":{},\"max_used\":{},\"min_headroom\":{},\"max_fill_permille\":{}}}",
        scenario.name(),
        round,
        owners.len(),
        max_used,
        min_headroom,
        max_fill_permille
    );
    Ok(())
}
