//! Readable summaries over the native configuration report.
use super::{describe, prompt, value_string};
use nxrs_build::config_edit::{Draft, Preview};
use serde_json::Value;

fn friendly_value(value: &str, kind: &str) -> String {
    match (kind, value) {
        ("bool" | "tristate", "y") => "On (y)".into(),
        ("bool" | "tristate", "n") => "Off (n)".into(),
        ("tristate", "m") => "Module (m)".into(),
        ("string", _) => format!("{value:?}"),
        _ => value.into(),
    }
}

fn counted(count: usize, noun: &str) -> String {
    format!("{count} {noun}{}", if count == 1 { "" } else { "s" })
}

pub(super) fn detail(
    name: &str,
    catalog: &Value,
    draft: &Draft,
    technical: bool,
    editable_pin: bool,
) -> String {
    let symbol = &catalog[name];
    let label = prompt(symbol);
    let title = if label.is_empty() {
        format!("CONFIG_{name}")
    } else {
        label.to_owned()
    };
    if technical {
        return format!(
            "{title}\nTechnical details · t Summary\n\n{}",
            describe(name, symbol)
        );
    }

    let kind = value_string(&symbol["type"]);
    let mut rows = vec![
        title,
        format!(
            "Current value: {}",
            friendly_value(value_string(&symbol["effective"]), kind)
        ),
    ];
    if let Some(value) = draft.pins.get(name) {
        rows.push(format!("Pending request: {value}"));
    } else if let Some(value) = draft.symbols.get(name) {
        // Draft string values already carry native quoting; do not quote them twice.
        let request = value.as_deref().map_or_else(
            || "remove the fragment override".into(),
            |value| friendly_value(value, if kind == "string" { "" } else { kind }),
        );
        rows.push(format!("Pending request: {request}"));
    }

    let owner = symbol["ownership"]["owner"].as_str();
    let unavailable = symbol["visibility"] == "n";
    if editable_pin {
        rows.push("Editable resource pin · Space to change".into());
    } else if let Some(owner) = owner {
        rows.push(format!("Read-only · owned by {owner}"));
    } else if unavailable {
        rows.push(
            if symbol["requires"]["value"] == "n" {
                "Unavailable · native requirements are not met"
            } else {
                "Unavailable · no visible prompt"
            }
            .into(),
        );
    } else if matches!(kind, "bool" | "tristate")
        && symbol["assignable"]
            .as_array()
            .is_some_and(|values| values.len() < 2)
    {
        rows.push("Value restricted by native conditions".into());
    } else {
        rows.push("Editable · Space to change".into());
    }

    let requires = &symbol["requires"];
    if let Some(expression) = requires["expression"].as_str().filter(|text| *text != "y") {
        rows.push(format!(
            "Requires {expression} ({})",
            value_string(&requires["value"])
        ));
    }
    if requires["value"] == "n" {
        if let Some(prerequisites) = symbol["prerequisites"]
            .as_array()
            .filter(|items| items.len() <= 3)
        {
            let inputs = prerequisites
                .iter()
                .filter_map(Value::as_str)
                .filter_map(|name| {
                    let input = catalog.get(name)?;
                    let label = prompt(input);
                    (!label.is_empty()).then(|| {
                        format!(
                            "{label}: {}",
                            friendly_value(
                                value_string(&input["effective"]),
                                value_string(&input["type"])
                            )
                        )
                    })
                })
                .collect::<Vec<_>>();
            if !inputs.is_empty() {
                // These are expression inputs, not a checklist to enable:
                // the native expression above retains alternatives and negation.
                rows.push(format!("Current inputs: {}", inputs.join("; ")));
            }
        }
    }
    // Direct dependencies and prompt visibility are different native conditions.
    // Preserve each expression rather than treating menu membership as a blocker
    // or flattening alternatives from several native definition locations.
    if unavailable && requires["value"] != "n" {
        let mut conditions = Vec::new();
        for node in symbol["locations"].as_array().into_iter().flatten() {
            if let Some(expression) = node["prompt_condition"]["expression"].as_str() {
                let condition = format!(
                    "Prompt condition: {expression} ({})",
                    value_string(&node["prompt_condition"]["value"])
                );
                if !conditions.contains(&condition) {
                    conditions.push(condition);
                }
            }
        }
        rows.extend(conditions);
    }
    for (field, label) in [("enabled_by", "Enabled by"), ("implied_by", "Suggested by")] {
        for entry in symbol[field].as_array().into_iter().flatten() {
            let active = |value: &Value| matches!(value.as_str(), Some("m" | "y"));
            if active(&entry["value"]) && active(&entry["when"]["value"]) {
                let source = value_string(&entry["symbol"]);
                let readable = prompt(&catalog[source]);
                let source_label = if readable.is_empty() {
                    format!("CONFIG_{source}")
                } else {
                    format!("{readable} (CONFIG_{source})")
                };
                rows.push(format!(
                    "{label}: {source_label} ({})",
                    value_string(&entry["value"])
                ));
            }
        }
    }

    if editable_pin {
        if let Some(owner) = owner {
            rows.push(format!("Owned by {owner}"));
        }
    }
    if let Some(source) = symbol["ownership"]["source"].as_str() {
        rows.push(format!(
            "{}: {source}",
            if editable_pin {
                "Save destination"
            } else {
                "Source"
            }
        ));
    } else if owner.is_none() {
        rows.push("Saves to native fragment · Review shows destinations after an edit".into());
    }

    let mut locations = Vec::new();
    let mut help = Vec::new();
    for node in symbol["locations"].as_array().into_iter().flatten() {
        let path = node["menu"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(" › ");
        if !path.is_empty() && !locations.contains(&path) {
            locations.push(path);
        }
        if let Some(text) = node["help"].as_str().filter(|text| !text.trim().is_empty()) {
            if !help.contains(&text) {
                help.push(text);
            }
        }
    }
    rows.extend(
        locations
            .into_iter()
            .map(|path| format!("Location: {path}")),
    );
    rows.push(
        if symbol["prerequisites"]
            .as_array()
            .is_some_and(|items| !items.is_empty())
        {
            "p Inspect prerequisites · t Technical details".into()
        } else {
            "t Technical details".into()
        },
    );
    for text in help {
        rows.push(format!("\nHelp\n{text}"));
    }
    rows.join("\n")
}

fn changed_symbols<'a>(
    baseline: Option<&'a Value>,
    report: &'a Value,
) -> impl Iterator<Item = (&'a str, &'a Value, &'a Value)> {
    report["symbols"]
        .as_object()
        .into_iter()
        .flat_map(move |symbols| {
            symbols.iter().filter_map(move |(name, symbol)| {
                let old = &baseline?["symbols"][name]["effective"];
                let new = &symbol["effective"];
                (old != new).then_some((name.as_str(), old, new))
            })
        })
}

pub(super) fn native_change_count(baseline: Option<&Value>, report: &Value) -> usize {
    changed_symbols(baseline, report).count()
}

pub(super) fn review(
    preview: Option<&Preview>,
    baseline: Option<&Value>,
    save_error: Option<&str>,
    pending: bool,
    retry: bool,
) -> String {
    let Some(preview) = preview else {
        return "Resolving the draft…".into();
    };
    let problems = preview.report["problems"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let problem_count = problems.len() + usize::from(save_error.is_some());
    let native_changes = if preview.report["symbols"].is_object()
        && baseline.is_some_and(|before| before["symbols"].is_object())
    {
        counted(
            native_change_count(baseline, &preview.report),
            "native change",
        )
    } else {
        "native changes unavailable".into()
    };
    let mut rows = Vec::new();
    if pending {
        rows.push("Resolving latest draft · previous preview below".into());
    }
    rows.push(format!(
        "{} · {native_changes} · {}",
        counted(preview.authored.len(), "source edit"),
        counted(problem_count, "problem"),
    ));
    rows.push(
        if retry {
            "Saved; sync failed · S Retry sync (sources are already saved)"
        } else if pending {
            "Wait for resolution before saving."
        } else if !problems.is_empty() {
            "Resolve the problems below before saving."
        } else if save_error.is_some() {
            "Save failed · review the error below before retrying."
        } else if preview.edits.is_empty() {
            "No source changes to save."
        } else {
            "Ready to save and sync · S Save & sync"
        }
        .into(),
    );

    if problem_count > 0 {
        rows.push("\nProblems to resolve".into());
        rows.extend(
            problems
                .iter()
                .filter_map(Value::as_str)
                .map(|problem| format!("  {problem}")),
        );
        if let Some(error) = save_error {
            rows.push(format!("  {error}"));
        }
    }
    rows.push(
        if retry {
            "\nSaved source edits"
        } else {
            "\nYour source edits"
        }
        .into(),
    );
    if preview.authored.is_empty() {
        rows.push("  None".into());
    } else {
        rows.push("Shared destinations affect every profile that uses them.".into());
        rows.extend(preview.authored.iter().map(|line| format!("  {line}")));
    }
    rows.push("\nResulting native changes".into());
    if !preview.report["symbols"].is_object() || baseline.is_none() {
        rows.push("  Unavailable until the draft resolves against a baseline.".into());
    } else {
        let changes = changed_symbols(baseline, &preview.report)
            .map(|(name, old, new)| {
                format!(
                    "  CONFIG_{name}: {} → {}",
                    value_string(old),
                    value_string(new)
                )
            })
            .collect::<Vec<_>>();
        if changes.is_empty() {
            rows.push("  None".into());
        } else {
            rows.extend(changes);
        }
    }
    rows.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use nxrs_build::config_edit::problem;
    use serde_json::json;

    #[test]
    fn unavailable_summary_preserves_native_alternatives_and_separates_location() {
        let catalog = json!({"TARGET":{
            "type":"bool", "effective":"n", "visibility":"n",
            "requires":{"expression":"CONFIG_A || CONFIG_B", "value":"n"},
            "prerequisites":["A", "B"],
            "locations":[{"prompt":"Optional subsystem", "menu":["Features", "Optional"],
                "help":"Long explanatory help follows the actual restriction."}]
        }, "A":{"type":"bool", "effective":"n", "locations":[{"prompt":"First route"}]},
            "B":{"type":"bool", "effective":"n", "locations":[{"prompt":"Second route"}]}});
        let summary = detail("TARGET", &catalog, &Draft::default(), false, false);
        assert!(summary.starts_with("Optional subsystem\nCurrent value: Off (n)"));
        assert!(summary.contains("Unavailable · native requirements are not met"));
        assert!(summary.contains("Requires CONFIG_A || CONFIG_B (n)"));
        assert!(summary.contains("Current inputs: First route: Off (n); Second route: Off (n)"));
        assert!(summary.contains("Location: Features › Optional"));
        assert!(summary.contains("p Inspect prerequisites"));
        assert!(summary.find("Requires").unwrap() < summary.find("Long explanatory help").unwrap());
        assert!(!summary.contains("Default"));
    }

    #[test]
    fn prompt_visibility_is_not_misreported_as_a_failed_direct_dependency() {
        let catalog = json!({"TARGET":{
            "type":"bool", "effective":"n", "visibility":"n",
            "requires":{"expression":"y", "value":"y"},
            "locations":[{"prompt":"Hidden feature", "menu":["Features"],
                "prompt_condition":{"expression":"CONFIG_EXPERT || CONFIG_TEST", "value":"n"}}]
        }});
        let summary = detail("TARGET", &catalog, &Draft::default(), false, false);
        assert!(summary.contains("Unavailable · no visible prompt"));
        assert!(summary.contains("Prompt condition: CONFIG_EXPERT || CONFIG_TEST (n)"));
        assert!(!summary.contains("native requirements are not met"));
        assert!(!summary.contains("Requires"));
    }

    #[test]
    fn resource_pin_ownership_and_string_values_are_presented_truthfully() {
        let catalog = json!({
            "PIN":{"type":"int", "effective":"12", "visibility":"y",
                "ownership":{"owner":"resources.i2c.kconfig.PIN", "source":"board.toml"}},
            "TEXT":{"type":"string", "effective":"n", "visibility":"y"}
        });
        let mut draft = Draft::default();
        draft.pins.insert("PIN".into(), 14);
        let editable = detail("PIN", &catalog, &draft, false, true);
        assert!(editable.contains("Editable resource pin"));
        assert!(editable.contains("Pending request: 14"));
        assert!(editable.contains("Save destination: board.toml"));
        assert!(!editable.contains("Read-only"));
        assert!(detail("PIN", &catalog, &draft, false, false).contains("Read-only"));
        assert!(detail("TEXT", &catalog, &draft, false, false).contains("Current value: \"n\""));
    }

    #[test]
    fn review_keeps_blockers_before_large_change_lists_and_retains_destinations() {
        let before = json!({"symbols":(0..40).map(|i| (format!("OPTION_{i:02}"), json!({"effective":"n"}))).collect::<serde_json::Map<_,_>>()});
        let after = json!({"symbols":(0..40).map(|i| (format!("OPTION_{i:02}"), json!({"effective":"y"}))).collect::<serde_json::Map<_,_>>(),
            "problems":["Requested value cannot take effect."]});
        let mut preview = problem(String::new());
        preview.report = after;
        preview
            .authored
            .push("product/native.conf\n  CONFIG_OPTION_00: n → y".into());
        let text = review(Some(&preview), Some(&before), None, false, false);
        assert!(text.starts_with("1 source edit · 40 native changes · 1 problem"));
        assert!(
            text.find("Requested value cannot take effect").unwrap()
                < text.find("Your source edits").unwrap()
        );
        assert!(text.contains("product/native.conf"));
        assert!(text.contains("CONFIG_OPTION_39: n → y"));
        assert!(!text.contains("Ready to save"));
    }

    #[test]
    fn pending_and_retry_reviews_identify_their_actual_state() {
        let mut preview = problem(String::new());
        preview.report = json!({"symbols":{}, "problems":[]});
        preview
            .authored
            .push("product/native.conf\n  CONFIG_DEBUG: n → y".into());
        preview
            .edits
            .insert("product/native.conf".into(), "CONFIG_DEBUG=y\n".into());
        let pending = review(Some(&preview), Some(&preview.report), None, true, false);
        assert!(pending.starts_with("Resolving latest draft · previous preview below"));
        assert!(!pending.contains("Ready to save"));
        let retry = review(
            Some(&preview),
            Some(&preview.report),
            Some("Compiler unavailable\nFull diagnostic"),
            false,
            true,
        );
        assert!(retry.contains("sources are already saved"));
        assert!(retry.contains("Saved source edits"));
        assert!(retry.contains("Compiler unavailable\nFull diagnostic"));
        assert!(!retry.contains("Ready to save"));
    }
}
