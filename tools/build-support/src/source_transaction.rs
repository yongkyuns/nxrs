//! A recoverable publication of configuration source files.
use crate::{hash_bytes, profile::SourceEdits, write_json, Result, TempDir};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    path::{Component, Path, PathBuf},
};

const DIRECTORY: &str = ".nxrs-config";
const JOURNAL: &str = "pending.json";

#[derive(Debug, Serialize, Deserialize)]
struct Edit {
    path: PathBuf,
    before: Option<String>,
    after: String,
}

pub fn check(root: &Path) -> Result<()> {
    let directory = state_directory(root, false)?;
    if read_source(&directory.join(JOURNAL))?.is_some() {
        return Err(
            "configuration source publication is pending; run cargo nxrs config recover".into(),
        );
    }
    Ok(())
}

fn state_directory(root: &Path, create: bool) -> Result<PathBuf> {
    let root = root.canonicalize().map_err(|e| e.to_string())?;
    let directory = root.join(DIRECTORY);
    match fs::symlink_metadata(&directory) {
        Ok(metadata) if metadata.is_dir() => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && create => {
            match fs::create_dir(&directory) {
                Ok(()) => {
                    sync(&root)?;
                }
                Err(error)
                    if error.kind() == std::io::ErrorKind::AlreadyExists
                        && fs::symlink_metadata(&directory)
                            .is_ok_and(|metadata| metadata.is_dir()) => {}
                Err(error) => return Err(format!("{}: {error}", directory.display())),
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        _ => {
            return Err(format!(
                "invalid configuration state directory {}",
                directory.display()
            ))
        }
    }
    Ok(directory)
}

pub fn validate_edit_path(root: &Path, path: &Path) -> Result<()> {
    let root = root.canonicalize().map_err(|e| e.to_string())?;
    let proper_extension = matches!(
        path.extension().and_then(|s| s.to_str()),
        Some("toml" | "conf")
    );
    let under_root = path.strip_prefix(&root).ok().is_some_and(|path| {
        path.components().all(|component| match component {
            Component::Normal(name) => name.to_str().is_some_and(|name| {
                !name.starts_with('.') && !["external", "target"].contains(&name)
            }),
            _ => false,
        })
    });
    let parent = path.parent().ok_or("missing source parent")?;
    if !under_root || !proper_extension || parent.canonicalize().ok().as_deref() != Some(parent) {
        return Err(format!(
            "{} is outside editable configuration sources",
            path.display()
        ));
    }
    read_source(path).map(|_| ())
}

fn read_source(path: &Path) -> Result<Option<String>> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_file() => crate::read(path).map(Some),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Ok(_) => Err(format!(
            "{}: expected a regular configuration source",
            path.display()
        )),
        Err(error) => Err(format!("{}: {error}", path.display())),
    }
}

fn replace(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .ok_or("configuration source requires a parent")?;
    let temporary = TempDir::new(parent)?;
    let candidate = temporary.0.join("source");
    fs::write(&candidate, bytes).map_err(|e| e.to_string())?;
    if let Ok(metadata) = fs::metadata(path) {
        fs::set_permissions(&candidate, metadata.permissions()).map_err(|e| e.to_string())?;
    }
    sync(&candidate)?;
    fs::rename(candidate, path).map_err(|e| format!("{}: {e}", path.display()))?;
    sync(parent)
}

fn sync(path: &Path) -> Result<()> {
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|e| format!("{}: {e}", path.display()))
}

struct SourceLock(File);

impl Drop for SourceLock {
    fn drop(&mut self) {
        // Explicitly release the lock even if another thread is between fork
        // and exec and temporarily holds the close-on-exec descriptor.
        let _ = self.0.unlock();
    }
}

fn lock(root: &Path) -> Result<(SourceLock, PathBuf)> {
    let directory = state_directory(root, true)?;
    let lock_path = directory.join("lock");
    read_source(&lock_path)?;
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(lock_path)
        .map_err(|e| e.to_string())?;
    file.try_lock()
        .map_err(|e| format!("configuration sources are being saved: {e}"))?;
    Ok((SourceLock(file), directory.join(JOURNAL)))
}

/// All observed inputs must still match when source publication starts.
pub fn publish(
    root: &Path,
    observations: &BTreeMap<PathBuf, Option<String>>,
    edits: &SourceEdits,
) -> Result<bool> {
    if edits.is_empty() {
        return Ok(false);
    }
    for path in edits.keys() {
        validate_edit_path(root, path)?;
    }
    let (_lock, journal) = lock(root)?;
    check(root)?;
    let edits = edits
        .iter()
        .filter_map(|(path, after)| {
            let before = observations.get(path);
            let edit = Edit {
                path: path.clone(),
                before: before.cloned().flatten(),
                after: after.clone(),
            };
            match before {
                Some(Some(digest)) if *digest == hash_bytes(after.as_bytes()) => None,
                Some(_) => Some(Ok(edit)),
                None => Some(Err(format!(
                    "missing source observation for {}",
                    path.display()
                ))),
            }
        })
        .collect::<Result<Vec<_>>>()?;
    if edits.is_empty() {
        return Ok(false);
    }
    for (path, digest) in observations {
        let current = read_source(path)?.map(|bytes| hash_bytes(bytes.as_bytes()));
        if current != *digest {
            return Err(format!(
                "{} changed while the draft was open; reload before saving",
                path.display()
            ));
        }
    }
    let staging = TempDir::new(journal.parent().unwrap())?;
    let filename = staging.0.join(JOURNAL);
    write_json(
        &filename,
        &serde_json::to_value(&edits).map_err(|e| e.to_string())?,
    )?;
    sync(&filename)?;
    fs::rename(filename, &journal).map_err(|e| e.to_string())?;
    sync(journal.parent().unwrap())
        .and_then(|_| finish(root, &journal, &edits))
        .map_err(|e| {
            format!("{e}. Configuration publication is pending; run cargo nxrs config recover")
        })?;
    Ok(true)
}

fn finish(root: &Path, journal: &Path, edits: &[Edit]) -> Result<()> {
    for edit in edits {
        validate_edit_path(root, &edit.path)?;
    }
    for edit in edits {
        let current = read_source(&edit.path)?;
        let digest = current.as_ref().map(|bytes| hash_bytes(bytes.as_bytes()));
        if digest != edit.before && current.as_deref() != Some(&edit.after) {
            return Err(format!(
                "{} changed outside the pending publication",
                edit.path.display()
            ));
        }
    }
    for edit in edits {
        let current = read_source(&edit.path)?;
        if current.as_deref() == Some(&edit.after) {
            // Recovery can find a renamed source whose parent was not synced.
            sync(edit.path.parent().unwrap())?;
            continue;
        }
        if current.as_ref().map(|bytes| hash_bytes(bytes.as_bytes())) != edit.before {
            return Err(format!(
                "{} changed during source publication",
                edit.path.display()
            ));
        }
        replace(&edit.path, edit.after.as_bytes())?;
    }
    crate::remove_file(journal)
}

pub fn recover(root: &Path) -> Result<()> {
    let (_lock, journal) = lock(root)?;
    let Some(contents) = read_source(&journal)? else {
        return Ok(());
    };
    let edits: Vec<Edit> = serde_json::from_str(&contents).map_err(|e| e.to_string())?;
    finish(root, &journal, &edits)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_edits_fail_and_a_partially_published_pair_can_be_recovered() {
        let temporary = TempDir::new(&std::env::temp_dir()).unwrap();
        let canonical = temporary.0.canonicalize().unwrap();
        let root = &canonical;
        let profile = root.join("profile.toml");
        let fragment = root.join("board.conf");
        fs::write(&profile, "original").unwrap();
        let observations = BTreeMap::from([
            (profile.clone(), Some(hash_bytes(b"original"))),
            (fragment.clone(), None),
        ]);
        let authored = SourceEdits::from([
            (profile.clone(), "nuttx-config = 'board.conf'\n".into()),
            (fragment.clone(), "CONFIG_DEBUG_FEATURES=y\n".into()),
        ]);
        fs::write(&profile, "operator edit").unwrap();
        assert!(publish(root, &observations, &authored)
            .unwrap_err()
            .contains("changed"));
        assert!(!fragment.exists());

        let (_guard, journal) = lock(root).unwrap();
        let edits: Vec<_> = authored
            .iter()
            .map(|(path, after)| Edit {
                path: path.clone(),
                before: observations[path].clone(),
                after: after.clone(),
            })
            .collect();
        write_json(&journal, &serde_json::to_value(edits).unwrap()).unwrap();
        fs::write(&fragment, &authored[&fragment]).unwrap();
        assert!(check(root).is_err());
        drop(_guard);
        assert!(recover(root).is_err());
        assert_eq!(crate::read(&profile).unwrap(), "operator edit");
        fs::write(&profile, "original").unwrap();
        recover(root).unwrap();
        check(root).unwrap();
        assert_eq!(crate::read(&profile).unwrap(), authored[&profile]);
        assert_eq!(crate::read(&fragment).unwrap(), authored[&fragment]);
    }

    #[test]
    fn recovery_rejects_an_outside_source_in_a_journal() {
        let temporary = TempDir::new(&std::env::temp_dir()).unwrap();
        let sibling = TempDir::new(&std::env::temp_dir()).unwrap();
        let root = temporary.0.canonicalize().unwrap();
        let outside = sibling.0.canonicalize().unwrap().join("outside.conf");
        let (guard, journal) = lock(&root).unwrap();
        let edit = Edit {
            path: outside.clone(),
            before: None,
            after: "injected".into(),
        };
        write_json(&journal, &serde_json::to_value(vec![edit]).unwrap()).unwrap();
        drop(guard);
        assert!(recover(&root).unwrap_err().contains("outside editable"));
        assert!(!outside.exists());
    }
}
