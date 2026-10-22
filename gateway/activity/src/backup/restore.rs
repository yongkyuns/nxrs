//! Restore to an empty preprovisioned tree. A durable intent gates visibility.
use super::process::Restic;
use super::{hex, private_dir, read_private, Config, Error};
use crate::store::Store;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File},
    io::Write,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Intent {
    version: u8,
    snapshot: String,
    repository_id: String,
    volume_uuid: String,
    cameras: Vec<String>,
    stage: String,
    ready: bool,
}
fn path(config: &Config) -> PathBuf {
    config.maintenance().join("restore.json")
}
fn sync(directory: &Path) -> Result<(), Error> {
    File::open(directory)?.sync_all()?;
    Ok(())
}
fn write(config: &Config, intent: &Intent, replace: bool) -> Result<(), Error> {
    let target = if replace {
        config.maintenance().join("restore.next")
    } else {
        path(config)
    };
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(&target)?;
    let bytes = serde_json::to_vec(intent).map_err(|_| Error::Recovery)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    if replace {
        fs::rename(target, path(config))?;
    }
    sync(&config.maintenance())
}
fn read(config: &Config, snapshot: &str) -> Result<Intent, Error> {
    let intent: Intent =
        serde_json::from_slice(&read_private(&path(config), 4096)?).map_err(|_| Error::Recovery)?;
    if intent.version != 1
        || intent.snapshot != snapshot
        || intent.repository_id != config.repository_id.as_deref().ok_or(Error::Invalid)?
        || intent.volume_uuid != config.volume().luks_uuid
        || intent.cameras != config.storage.camera_ids
        || !hex(&intent.stage, 32)
    {
        return Err(Error::Recovery);
    }
    Ok(intent)
}
fn stage(config: &Config, intent: &Intent) -> PathBuf {
    config
        .maintenance()
        .join(format!("restore-{}", intent.stage))
}
fn require_empty(config: &Config) -> Result<(), Error> {
    private_dir(&config.storage.directory, config.volume().operator_uid()?)?;
    if fs::read_dir(&config.storage.directory)?.next().is_some() {
        return Err(Error::Recovery);
    }
    Ok(())
}
fn remove_stage(config: &Config, intent: &Intent) -> Result<(), Error> {
    let directory = stage(config, intent);
    match fs::symlink_metadata(&directory) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(Error::Recovery),
        Ok(_) => {
            private_dir(&directory, config.volume().operator_uid()?)?;
            fs::remove_dir_all(directory)?;
            Ok(())
        }
    }
}
fn root_entries(config: &Config) -> Result<Vec<PathBuf>, Error> {
    let entries = fs::read_dir(&config.storage.directory)?
        .take(5 + config.storage.camera_ids.len() * 2)
        .map(|entry| entry.map(|v| v.path()))
        .collect::<Result<Vec<_>, _>>()?;
    if entries.len() > 4 + config.storage.camera_ids.len() * 2 {
        return Err(Error::Recovery);
    }
    for entry in &entries {
        let name = entry
            .file_name()
            .and_then(|v| v.to_str())
            .ok_or(Error::Recovery)?;
        if !["events", "staging", "spool", "store.lock"].contains(&name)
            && !config.storage.camera_ids.iter().any(|camera| {
                name == format!("worker-{camera}.lock") || name == format!("health-{camera}.json")
            })
        {
            return Err(Error::Recovery);
        }
    }
    Ok(entries)
}
fn discard_candidate(config: &Config) -> Result<(), Error> {
    private_dir(&config.storage.directory, config.volume().operator_uid()?)?;
    for entry in root_entries(config)? {
        let metadata = fs::symlink_metadata(&entry)?;
        if metadata.is_dir() {
            fs::remove_dir_all(entry)?;
        } else {
            fs::remove_file(entry)?;
        }
    }
    sync(&config.storage.directory)
}
fn durable_tree(root: &Path, depth: u8) -> Result<(), Error> {
    if depth > 3 {
        return Err(Error::Recovery);
    }
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            durable_tree(&entry.path(), depth + 1)?;
        } else {
            File::open(entry.path())?.sync_all()?;
        }
    }
    sync(root)
}
fn fresh(config: &Config, snapshot: &str) -> Result<Intent, Error> {
    let mut entropy = [0; 16];
    getrandom::fill(&mut entropy).map_err(|_| Error::Io)?;
    Ok(Intent {
        version: 1,
        snapshot: snapshot.into(),
        repository_id: config.repository_id.clone().ok_or(Error::Invalid)?,
        volume_uuid: config.volume().luks_uuid.clone(),
        cameras: config.storage.camera_ids.clone(),
        stage: entropy.iter().map(|v| format!("{v:02x}")).collect(),
        ready: false,
    })
}
/// The exclusive lifetime gate belongs to execute(); it outlives child retirement.
pub(super) async fn run(
    config: &Config,
    restic: &Restic<'_>,
    snapshot: &str,
    resume: bool,
) -> Result<(), Error> {
    let previous = if resume {
        Some(read(config, snapshot)?)
    } else {
        config.volume().require_no_restore()?;
        require_empty(config)?;
        None
    };
    // Authenticate the complete source before discarding an unserved interrupted candidate.
    restic.check().await?;
    let listing = restic
        .bounded(&["ls", &format!("{snapshot}:/activity")], 16 * 1024 * 1024)
        .await?;
    super::manifest::validate(config, &listing)?;
    drop(listing);
    config.storage.verify_protection()?;
    if let Some(previous) = previous {
        if previous.ready {
            discard_candidate(config)?;
        } else {
            require_empty(config)?;
        }
        remove_stage(config, &previous)?;
        let next = config.maintenance().join("restore.next");
        if next.exists() {
            read_private(&next, 4096)?;
            fs::remove_file(next)?;
        }
    }
    let mut intent = fresh(config, snapshot)?;
    write(config, &intent, resume)?;
    let directory = stage(config, &intent);
    let result = async {
        fs::DirBuilder::new().mode(0o700).create(&directory)?;
        let selector = format!("{snapshot}:/activity");
        let target = directory.to_str().ok_or(Error::Invalid)?;
        restic
            .command(&[
                "restore",
                &selector,
                "--target",
                target,
                "--verify",
                "--overwrite",
                "never",
                "--exclude-xattr",
                "*",
            ])
            .await?;
        Store::inspect_snapshot(&config.storage, &directory)?;
        private_dir(&directory, config.volume().operator_uid()?)?;
        durable_tree(&directory, 0)?;
        config.storage.verify_protection()?;
        require_empty(config)?;
        // This durable state precedes every publication rename. All readers stay
        // blocked by the intent until every entry and both directories are synced.
        intent.ready = true;
        write(config, &intent, true)?;
        for entry in fs::read_dir(&directory)? {
            let entry = entry?;
            fs::rename(
                entry.path(),
                config.storage.directory.join(entry.file_name()),
            )?;
        }
        sync(&config.storage.directory)?;
        fs::remove_dir(&directory)?;
        sync(&config.maintenance())?;
        fs::remove_file(path(config))?;
        sync(&config.maintenance())?;
        Ok(())
    }
    .await;
    if result.is_err() && !intent.ready {
        // The native child has retired before this future returns an error.
        // The original target was empty; no committed recording can be deleted.
        if remove_stage(config, &intent).is_err() {
            return Err(Error::Recovery);
        }
        fs::remove_file(path(config))?;
        sync(&config.maintenance())?;
    }
    if result.is_err() && intent.ready {
        return Err(Error::Recovery);
    }
    result
}
