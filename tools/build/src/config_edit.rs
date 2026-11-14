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
        // App includes the opening metadata and CLI overrides. Do not pair it
        // with a newer manifest when another draft reloads the profile.
        observations.insert(
            app.manifest.clone(),
            Some(hash_bytes(read(&app.manifest)?.as_bytes())),
        );
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
        self.observed_source(&self.app.manifest)?;
        let (edits, authored) = self.source_edits(draft)?;
        let resolved = self
            .profile
            .with_source_edits(&self.root, &edits)
            .and_then(|profile| {
                let report = inspection.inspect(&self.root, &self.app, &profile, &self.options)?;
                Ok((report, profile.input_fingerprint()))
            });
        self.observed_source(&self.app.manifest)?;
        let (report, profile_fingerprint) =
            resolved.unwrap_or_else(|error| (json!({"problems":[error]}), None));
        Ok(Preview {
            edits,
            authored,
            report,
            profile_fingerprint,
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
        self.observed_source(&self.app.manifest)?;
        source_transaction::publish(&self.root, &self.observations, &preview.edits)
    }

    fn saved_profile(&self, preview: &Preview) -> Result<Profile> {
        check_report(&preview.report)?;
        self.observed_source(&self.app.manifest)?;
        let resolved = self
            .profile
            .with_source_edits(&self.root, &SourceEdits::new())?;
        if resolved.input_fingerprint() != preview.profile_fingerprint {
            return Err("configuration sources differ from the reviewed draft; reopen to sync current sources".into());
        }
        self.observed_source(&self.app.manifest)?;
        Ok(resolved)
    }

    /// Rebase the next draft on the reviewed sources after Save & sync succeeds.
    /// Keep the CLI-resolved application and options for the entire session.
    pub fn refresh(&self, preview: &Preview) -> Result<Self> {
        let refreshed = Self::new(
            self.root.clone(),
            self.app.clone(),
            self.saved_profile(preview)?,
            self.out.clone(),
            self.options.clone(),
        )?;
        if refreshed.observations.get(&self.app.manifest)
            != self.observations.get(&self.app.manifest)
        {
            return Err(format!(
                "{} changed while the draft was open; reopen before saving",
                self.app.manifest.display()
            ));
        }
        Ok(refreshed)
    }

    pub fn sync(&self, preview: &Preview, inspection: &mut nuttx::Inspection) -> Result<()> {
        let resolved = self.saved_profile(preview)?;
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
        self.observed_source(&self.app.manifest)?;
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
    use std::fs;

    fn session(fragment: Option<&str>) -> (crate::TempDir, Session) {
        let temporary = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let root = temporary.0.canonicalize().unwrap();
        let platform = root.join("platform/board");
        fs::create_dir_all(&platform).unwrap();
        fs::create_dir_all(root.join("app/probe")).unwrap();
        fs::write(root.join("Cargo.toml"), "[workspace]\n").unwrap();
        let mut source = "# profile notes\nname='board'\nboard='test:nsh'\n".to_owned();
        if let Some(contents) = fragment {
            source.push_str("nuttx-config='native.conf'\n");
            fs::write(platform.join("native.conf"), contents).unwrap();
        }
        source.push_str("[hal]\n");
        fs::write(platform.join("profile.toml"), source).unwrap();
        let manifest = root.join("app/probe/Cargo.toml");
        fs::write(
            &manifest,
            "[package]\nname='probe'\nversion='0.1.0'\n\
             [package.metadata.nxrs.firmware]\ncommand='probe'\npriority=100\nstack-size=4096\n",
        )
        .unwrap();
        let app = App::load(&manifest, None).unwrap();
        let profile = Profile::resolve(&root, "board").unwrap();
        let out = root.join("target/config");
        let session = Session::new(root, app, profile, out, Options::default()).unwrap();
        (temporary, session)
    }

    // Exercise source publication and refresh without staging a native SDK.
    // Native inspection and effective configuration validation have their own tests.
    fn source_preview(session: &Session, draft: &Draft) -> Preview {
        let (edits, authored) = session.source_edits(draft).unwrap();
        let profile = session
            .profile
            .with_source_edits(&session.root, &edits)
            .unwrap();
        Preview {
            report: json!({"problems": []}),
            edits,
            authored,
            profile_fingerprint: profile.input_fingerprint(),
        }
    }

    fn symbol(name: &str, value: Option<&str>) -> Draft {
        Draft {
            symbols: BTreeMap::from([(name.into(), value.map(str::to_owned))]),
            ..Draft::default()
        }
    }

    #[test]
    fn refreshed_session_publishes_again_and_preserves_cli_selection() {
        let (_temporary, mut original) = session(None);
        original.app.bin = "selected-bin".into();
        original.app.command = "cli_command".into();
        original.app.priority = 120;
        original.app.stack = 8192;
        original.options.features = vec!["extra".into()];
        original.options.no_default_features = true;
        original.options.set = vec!["PRIORITY=123".into()];
        let profile_path = original.profile.sources.last().unwrap().clone();
        let profile_source = read(&profile_path).unwrap();
        let first = source_preview(&original, &symbol("FIRST", Some("y")));
        assert_eq!(first.edits.len(), 2);
        assert!(original.publish(&first).unwrap());

        let refreshed = original.refresh(&first).unwrap();
        assert!(refreshed.reference.is_none());
        assert_eq!(
            refreshed.profile.native_config().unwrap().contents,
            "CONFIG_FIRST=y\n"
        );
        assert_eq!(
            read(&profile_path).unwrap(),
            format!("nuttx-config = \"board.conf\"\n{profile_source}")
        );
        assert_eq!(refreshed.app.manifest, original.app.manifest);
        assert_eq!(refreshed.app.bin, "selected-bin");
        assert_eq!(refreshed.app.command, "cli_command");
        assert_eq!(refreshed.app.priority, 120);
        assert_eq!(refreshed.app.stack, 8192);
        assert_eq!(refreshed.options.features, ["extra"]);
        assert!(refreshed.options.no_default_features);
        assert_eq!(refreshed.options.set, ["PRIORITY=123"]);
        assert_eq!(refreshed.out, original.out);
        assert_eq!(
            refreshed.observations[&refreshed.app.manifest],
            original.observations[&original.app.manifest]
        );

        let second = source_preview(&refreshed, &symbol("FIRST", Some("n")));
        assert_eq!(second.edits.len(), 1);
        assert!(second.edits.contains_key(&refreshed.fragment));
        assert!(refreshed.publish(&second).unwrap());
        let refreshed = refreshed.refresh(&second).unwrap();
        assert_eq!(
            refreshed.profile.native_config().unwrap().contents,
            "# CONFIG_FIRST is not set\n"
        );
        assert_eq!(
            refreshed.observations[&refreshed.fragment],
            Some(hash_bytes(read(&refreshed.fragment).unwrap().as_bytes()))
        );
    }

    #[test]
    fn no_op_refresh_preserves_comments_without_publication() {
        let contents = "# owner notes\r\nCONFIG_FIRST=y\r\n# CONFIG_SECOND is not set\r\n";
        let (_temporary, original) = session(Some(contents));
        let profile_path = original.profile.sources.last().unwrap();
        let profile_source = read(profile_path).unwrap();
        let preview = source_preview(&original, &symbol("FIRST", Some("y")));
        assert!(preview.edits.is_empty());
        assert!(preview.authored.is_empty());
        assert!(!original.publish(&preview).unwrap());
        let refreshed = original.refresh(&preview).unwrap();
        assert_eq!(
            refreshed.profile.input_fingerprint(),
            original.profile.input_fingerprint()
        );
        assert_eq!(read(profile_path).unwrap(), profile_source);
        assert_eq!(read(&refreshed.fragment).unwrap(), contents);
        assert!(!refreshed.root.join(".nxrs-config").exists());
        assert!(!refreshed.out.exists());

        let reset = source_preview(&refreshed, &symbol("FIRST", None));
        assert!(refreshed.publish(&reset).unwrap());
        let refreshed = refreshed.refresh(&reset).unwrap();
        assert_eq!(
            read(&refreshed.fragment).unwrap(),
            "# owner notes\r\n# CONFIG_SECOND is not set\r\n"
        );
    }

    #[test]
    fn refresh_requires_the_exact_reviewed_source_bytes() {
        let (_temporary, original) = session(Some("CONFIG_FIRST=y\n"));
        let preview = source_preview(&original, &symbol("FIRST", Some("n")));
        assert!(original
            .refresh(&preview)
            .err()
            .unwrap()
            .contains("differ from the reviewed draft"));
        assert!(original.publish(&preview).unwrap());
        fs::write(
            &original.fragment,
            "# externally added comment\n# CONFIG_FIRST is not set\n",
        )
        .unwrap();
        assert!(original
            .refresh(&preview)
            .err()
            .unwrap()
            .contains("differ from the reviewed draft"));
    }

    #[test]
    fn app_manifest_change_is_rejected_before_another_preview_or_save() {
        let (_temporary, original) = session(Some("CONFIG_FIRST=y\n"));
        let draft = symbol("FIRST", Some("n"));
        let preview = source_preview(&original, &draft);
        let no_op = source_preview(&original, &Draft::default());
        let source = read(&original.app.manifest).unwrap();
        fs::write(
            &original.app.manifest,
            source.replace("stack-size=4096", "stack-size=8192"),
        )
        .unwrap();
        let mut inspection = nuttx::Inspection::new();
        for error in [
            original.preview(&draft, &mut inspection).err().unwrap(),
            original.publish(&preview).err().unwrap(),
            original.publish(&no_op).err().unwrap(),
            original.sync(&preview, &mut inspection).err().unwrap(),
            original.refresh(&preview).err().unwrap(),
        ] {
            assert!(
                error.contains(&original.app.manifest.display().to_string()),
                "{error}"
            );
            assert!(
                error.contains("changed while the draft was open"),
                "{error}"
            );
        }
        assert_eq!(read(&original.fragment).unwrap(), "CONFIG_FIRST=y\n");
        assert!(!original.root.join(".nxrs-config").exists());
        assert!(!original.out.exists());
    }

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
