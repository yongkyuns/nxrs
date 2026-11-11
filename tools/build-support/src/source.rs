//! Verification for already prepared, pinned native SDK files.
use crate::{hash, read_json, Result};
use std::path::{Component, Path};

fn safe_path(source: &Path, path: &str) -> Result<std::path::PathBuf> {
    let relative = Path::new(path);
    if relative
        .components()
        .any(|c| !matches!(c, Component::Normal(_)))
        || path.contains('\\')
    {
        return Err(format!("unsafe patch path: {path}"));
    }
    let p = source.join(relative);
    let mut ancestor = p.as_path();
    while !ancestor.exists() {
        ancestor = ancestor.parent().ok_or("patch path needs parent")?;
    }
    if !ancestor
        .canonicalize()
        .map_err(|e| e.to_string())?
        .starts_with(source)
    {
        return Err("patch path escapes isolated source".into());
    }
    Ok(p)
}
/// A pinned file inventory permits SDK preparation without copying unrelated vendor tooling.
pub fn verify_inventory(ledger: &Path, destination: &Path) -> Result<()> {
    let pin = read_json(ledger)?;
    let identity = read_json(&destination.join("prepared.json"))?;
    if identity["ledger_sha256"] != hash(ledger)? || identity["files"] != pin["prepared_files"] {
        return Err("prepared SDK inventory differs from pinned ledger".into());
    }
    for (name, expected) in identity["files"]
        .as_object()
        .ok_or("invalid prepared inventory")?
    {
        let path = safe_path(
            &destination.canonicalize().map_err(|e| e.to_string())?,
            name,
        )?;
        if hash(&path)? != expected.as_str().ok_or("invalid prepared SDK hash")? {
            return Err(format!("prepared SDK changed: {name}"));
        }
    }
    Ok(())
}
