//! Authored native and resource edits for a single configuration session.
use crate::{
    build::{App, Options},
    hash_bytes, nuttx,
    nuttx_config::{assignment, check_report, line, verify_preview},
    profile::{Profile, SourceEdits},
    read, Result,
};
use nxrs_build_support::source_transaction;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};
use toml_edit::{DocumentMut, Item};

#[derive(Clone, Default)]
pub struct Draft {
    pub symbols: BTreeMap<String, Option<String>>,
    pub pins: BTreeMap<String, u64>,
}

pub struct Session {
    pub root: PathBuf,
    pub app: App,
    pub profile: Profile,
    pub out: PathBuf,
    pub options: Options,
    observations: BTreeMap<PathBuf, Option<String>>,
    fragment: PathBuf,
    reference: Option<(PathBuf, String)>,
}

pub struct Preview {
    pub report: Value,
    pub edits: SourceEdits,
    pub authored: Vec<String>,
    profile_fingerprint: Option<String>,
}

impl Session {
    pub fn new(
        root: PathBuf,
        app: App,
        profile: Profile,
        out: PathBuf,
        options: Options,
    ) -> Result<Self> {
        let mut observations: BTreeMap<_, _> = profile
            .source_hashes()?
            .into_iter()
            .map(|source| {
                let path = source["path"]
                    .as_str()
                    .ok_or("missing profile source path")?;
                let digest = source["sha256"]
                    .as_str()
                    .ok_or("missing profile source digest")?;
                Ok((PathBuf::from(path), Some(digest.to_owned())))
            })
            .collect::<Result<_>>()?;
        let (fragment, reference) = if let Some(fragment) = profile.native_config() {
            (fragment.path.clone(), None)
        } else {
            let selected = profile
                .sources
                .last()
                .ok_or("missing selected profile file")?;
            let name = format!("{}.conf", profile.name());
            let candidate = selected.with_file_name(&name);
            if candidate.try_exists().map_err(|e| e.to_string())? {
                return Err(format!(
                    "{} already exists; declare it with nuttx-config before editing",
                    candidate.display()
                ));
            }
            observations.insert(candidate.clone(), None);
            (candidate, Some((selected.clone(), name)))
        };
        Ok(Self {
            root,
            app,
            profile,
            out,
            options,
            observations,
            fragment,
            reference,
        })
    }

    pub fn preview(&self, draft: &Draft, inspection: &mut nuttx::Inspection) -> Result<Preview> {
        let (edits, authored) = self.source_edits(draft)?;
        let profile = self.profile.with_source_edits(&self.root, &edits)?;
        let report = inspection.inspect(&self.root, &self.app, &profile, &self.options)?;
        Ok(Preview {
            edits,
            authored,
            report,
            profile_fingerprint: profile.input_fingerprint(),
        })
    }

    fn source_edits(&self, draft: &Draft) -> Result<(SourceEdits, Vec<String>)> {
        let mut changes = SourceEdits::new();
        let mut authored = Vec::new();
        if !draft.symbols.is_empty() {
            source_transaction::validate_edit_path(&self.root, &self.fragment)?;
            let old = self
                .profile
                .native_config()
                .map(|fragment| fragment.contents.as_str())
                .unwrap_or("");
            let edited = edit_fragment(old, &draft.symbols);
            if edited != old {
                if let Some((profile, name)) = &self.reference {
                    let source = self.observed_source(profile)?;
                    authored.push(format!(
                        "{}\n  + nuttx-config = {name:?}",
                        profile.display()
                    ));
                    changes.insert(
                        profile.clone(),
                        format!("nuttx-config = {name:?}\n{source}"),
                    );
                }
                for (name, value) in &draft.symbols {
                    let previous = old
                        .lines()
                        .filter_map(|source| assignment(source.trim()))
                        .find_map(|(key, value)| (key == name).then_some(value));
                    if previous != value.as_deref() {
                        authored.push(format!(
                            "{}\n  CONFIG_{name}: {} → {}",
                            self.fragment.display(),
                            previous.unwrap_or("(inherited)"),
                            value.as_deref().unwrap_or("(inherited)")
                        ));
                    }
                }
                changes.insert(self.fragment.clone(), edited);
            }
        }
        let resources = &self.profile.data["resources"];
        for (symbol, value) in &draft.pins {
            let (id, owner, previous) = self.pin_owner(symbol)?;
            if previous == *value {
                continue;
            }
            let source = match changes.get(owner) {
                Some(text) => text.clone(),
                None => self.observed_source(owner)?,
            };
            let mut doc = source.parse::<DocumentMut>().map_err(|e| e.to_string())?;
            let table = doc
                .get_mut("resources")
                .and_then(Item::as_table_like_mut)
                .and_then(|table| table.get_mut(id))
                .and_then(Item::as_table_like_mut)
                .and_then(|table| table.get_mut("kconfig"))
                .and_then(Item::as_table_like_mut)
                .ok_or_else(|| {
                    format!("{} no longer owns resources.{id}.kconfig", owner.display())
                })?;
            let item = table
                .get_mut(symbol)
                .and_then(Item::as_value_mut)
                .ok_or_else(|| format!("{} no longer owns {symbol}", owner.display()))?;
            let decor = item.decor().clone();
            *item = toml_edit::Value::from(i64::try_from(*value).map_err(|e| e.to_string())?);
            *item.decor_mut() = decor;
            authored.push(format!(
                "{}\n  {} /resources/{id}/kconfig/{symbol}: {previous} → {value}",
                owner.display(),
                resources[id]["device"].as_str().unwrap_or("")
            ));
            changes.insert(owner.to_path_buf(), doc.to_string());
        }
        Ok((changes, authored))
    }

    fn observed_source(&self, path: &Path) -> Result<String> {
        let contents = read(path)?;
        if self.observations.get(path).and_then(Option::as_deref)
            != Some(&hash_bytes(contents.as_bytes()))
        {
            return Err(format!(
                "{} changed while the draft was open; reopen before saving",
                path.display()
            ));
        }
        Ok(contents)
    }

    pub fn pin_owner(&self, key: &str) -> Result<(&str, &Path, u64)> {
        let resources = self.profile.data["resources"]
            .as_object()
            .ok_or("no admitted NuttX resources")?;
        let selections = self
            .profile
            .data
            .pointer("/resolved-nuttx-resources/selections")
            .and_then(Value::as_object)
            .ok_or("no admitted NuttX resource selections")?;
        for (id, selection) in selections {
            if matches!(resources[id]["kind"].as_str(), Some("i2c" | "uart")) {
                if let Some(value) = selection["kconfig"][key].as_u64() {
                    let field = format!("resources.{id}.kconfig.{key}");
                    let path = self
                        .profile
                        .source_of(&field)
                        .ok_or("missing resource source")?;
                    source_transaction::validate_edit_path(&self.root, path)?;
                    return Ok((id, path, value));
                }
            }
        }
        Err(format!("CONFIG_{key} has no editable I2C or UART pin"))
    }

    pub fn publish(&self, preview: &Preview) -> Result<bool> {
        check_report(&preview.report)?;
        source_transaction::publish(&self.root, &self.observations, &preview.edits)
    }

    pub fn sync(&self, preview: &Preview, inspection: &mut nuttx::Inspection) -> Result<()> {
        let resolved = self
            .profile
            .with_source_edits(&self.root, &SourceEdits::new())?;
        if resolved.input_fingerprint() != preview.profile_fingerprint {
            return Err("configuration sources differ from the reviewed draft; reopen to sync current sources".into());
        }
        let saved = inspection.inspect(&self.root, &self.app, &resolved, &self.options)?;
        check_report(&saved)?;
        let config = |report: &Value| {
            report["config"]
                .as_str()
                .map(str::to_owned)
                .ok_or("missing effective configuration")
        };
        verify_preview(&config(&preview.report)?, &config(&saved)?)?;
        nuttx::prepare_config(
            &self.root,
            &self.app,
            &resolved,
            &self.out,
            &self.options,
            &config(&saved)?,
        )?;
        Ok(())
    }
}

fn edit_fragment(original: &str, edits: &BTreeMap<String, Option<String>>) -> String {
    let mut pending = edits.clone();
    let mut result = String::new();
    for text in original.split_inclusive('\n') {
        if let Some((key, old)) = assignment(text.trim()) {
            if let Some(value) = edits.get(key) {
                if pending.remove(key).is_some() {
                    if let Some(value) = value {
                        if value == old {
                            result.push_str(text);
                        } else {
                            result.push_str(&line(key, value));
                            result.push_str(if text.ends_with("\r\n") {
                                "\r\n"
                            } else if text.ends_with('\n') {
                                "\n"
                            } else {
                                ""
                            });
                        }
                    }
                }
                continue;
            }
        }
        result.push_str(text);
    }
    for (key, value) in pending {
        if let Some(value) = value {
            if !result.is_empty() && !result.ends_with('\n') {
                result.push('\n');
            }
            result.push_str(&line(&key, &value));
            result.push('\n');
        }
    }
    result
}

pub fn problem(message: String) -> Preview {
    Preview {
        report: json!({"problems":[message]}),
        edits: SourceEdits::new(),
        authored: vec![],
        profile_fingerprint: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reset_preserves_unrelated_assignments_and_comments() {
        let original = "# owner notes\r\nCONFIG_FIRST=y\r\n# CONFIG_SECOND is not set\r\n";
        let reset = edit_fragment(original, &BTreeMap::from([("FIRST".into(), None)]));
        assert_eq!(reset, "# owner notes\r\n# CONFIG_SECOND is not set\r\n");
        assert_eq!(
            edit_fragment(
                &reset,
                &BTreeMap::from([("SECOND".into(), Some("n".into()))])
            ),
            reset
        );
    }
}
