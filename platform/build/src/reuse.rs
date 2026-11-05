//! Reuse owned build directories only while their inputs and retained bytes match.
//!
//! Cargo and Make still decide which compilation units need rebuilding. This
//! module only admits a previously completed SDK/native configuration, and never
//! treats cached compilation state as evidence that the current build succeeded.
use crate::{hash, read_json, write_json, Result};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

const RECORD: &str = ".nxrs-build-cache.json";

fn visit(
    root: &Path,
    path: &Path,
    seen: &mut BTreeSet<PathBuf>,
    files: &mut BTreeMap<String, String>,
) -> Result<()> {
    let relative = path.strip_prefix(root).map_err(|e| e.to_string())?;
    let metadata = fs::symlink_metadata(path).map_err(|e| format!("{}: {e}", path.display()))?;
    if metadata.is_symlink() {
        let target = fs::read_link(path).map_err(|e| e.to_string())?;
        files.insert(
            relative.to_string_lossy().into_owned(),
            format!("symlink:{}", target.display()),
        );
    }
    let resolved = path
        .canonicalize()
        .map_err(|e| format!("{}: {e}", path.display()))?;
    if !resolved.starts_with(root) {
        return Err(format!(
            "cached build input escapes its owned tree: {}",
            path.display()
        ));
    }
    if !seen.insert(resolved) {
        return Ok(());
    }
    if path.is_dir() {
        let mut entries = fs::read_dir(path)
            .map_err(|e| e.to_string())?
            .map(|e| e.map(|e| e.path()).map_err(|e| e.to_string()))
            .collect::<Result<Vec<_>>>()?;
        entries.sort();
        for entry in entries {
            // A pinned vendor checkout's metadata is not a compiler input.
            if entry.file_name().is_some_and(|name| name == ".git") {
                continue;
            }
            visit(root, &entry, seen, files)?;
        }
    } else if path.is_file() {
        files.insert(format!("{}:bytes", relative.display()), hash(path)?);
    } else {
        return Err(format!("unsupported cached input: {}", path.display()));
    }
    Ok(())
}

/// Content identity, including additions, removals and symlink destinations.
pub fn snapshot(root: &Path, paths: &[&str]) -> Result<Value> {
    let root = root.canonicalize().map_err(|e| e.to_string())?;
    let mut files = BTreeMap::new();
    let mut seen = BTreeSet::new();
    for name in paths {
        visit(&root, &root.join(name), &mut seen, &mut files)?;
    }
    Ok(json!(files))
}

/// Begin a build. An interrupted build has no completed reuse record.
pub fn begin(out: &Path, inputs: &Value, directories: &[&str], clean: bool) -> Result<bool> {
    let record = out.join(RECORD);
    let reusable = !clean
        && read_json(&record).ok().is_some_and(|previous| {
            previous["schema"] == 1
                && previous["inputs"] == *inputs
                && snapshot(out, directories).is_ok_and(|files| files == previous["files"])
        });
    crate::remove_file(&record)?;
    if !reusable {
        for name in directories {
            let path = out.join(name);
            if path
                .symlink_metadata()
                .is_ok_and(|metadata| metadata.is_dir())
            {
                crate::remove_dir(&path)?;
            } else {
                crate::remove_file(&path)?;
            }
        }
    }
    Ok(reusable)
}

pub fn complete(out: &Path, inputs: &Value, directories: &[&str]) -> Result<()> {
    write_json(
        &out.join(RECORD),
        &json!({"schema":1,"inputs":inputs,"files":snapshot(out,directories)?}),
    )
}

/// Avoid touching an unchanged generated input and defeating Cargo/Make freshness.
pub fn write_if_changed(path: &Path, bytes: &[u8]) -> Result<()> {
    if path
        .symlink_metadata()
        .is_ok_and(|metadata| metadata.is_symlink())
    {
        crate::remove_file(path)?;
    }
    if fs::read(path).is_ok_and(|old| old == bytes) {
        return Ok(());
    }
    fs::write(path, bytes).map_err(|e| format!("{}: {e}", path.display()))
}

pub fn copy_if_changed(source: &Path, destination: &Path) -> Result<()> {
    write_if_changed(destination, &fs::read(source).map_err(|e| e.to_string())?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unchanged_native_and_sdk_bytes_are_reused_but_mutations_are_not() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let out = &temp.0;
        let inputs = json!({"profile":"fixture","features":["one"],"compiler":"pinned"});
        for directory in ["native", "sdk"] {
            fs::create_dir(out.join(directory)).unwrap();
            fs::write(out.join(directory).join("input.h"), "first").unwrap();
        }
        let directories = ["native", "sdk"];
        complete(out, &inputs, &directories).unwrap();
        assert!(begin(out, &inputs, &directories, false).unwrap());
        assert!(out.join("native/input.h").is_file());
        // Interrupted builds may retain bytes, but cannot qualify their reuse.
        assert!(!begin(out, &inputs, &directories, false).unwrap());
        assert!(!out.join("native").exists());
        for directory in directories {
            fs::create_dir(out.join(directory)).unwrap();
            fs::write(out.join(directory).join("input.h"), "first").unwrap();
        }
        complete(out, &inputs, &directories).unwrap();
        fs::write(out.join("native/input.h"), "changed ABI").unwrap();
        assert!(!begin(out, &inputs, &directories, false).unwrap());
    }

    #[test]
    fn changed_configuration_and_explicit_clean_discard_compilation_state() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        for clean in [false, true] {
            fs::create_dir(temp.0.join("native")).unwrap();
            fs::write(temp.0.join("native/input.c"), "source").unwrap();
            complete(&temp.0, &json!({"sdk":"one"}), &["native"]).unwrap();
            assert!(!begin(
                &temp.0,
                &json!({"sdk":if clean {"one"} else {"two"}}),
                &["native"],
                clean
            )
            .unwrap());
            assert!(!temp.0.join("native").exists());
        }
    }

    #[test]
    fn unchanged_generated_inputs_preserve_their_modification_time() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let file = temp.0.join("flags.json");
        write_if_changed(&file, b"same").unwrap();
        let before = file.metadata().unwrap().modified().unwrap();
        write_if_changed(&file, b"same").unwrap();
        assert_eq!(file.metadata().unwrap().modified().unwrap(), before);
        write_if_changed(&file, b"different").unwrap();
        assert_eq!(fs::read(file).unwrap(), b"different");
    }
}
