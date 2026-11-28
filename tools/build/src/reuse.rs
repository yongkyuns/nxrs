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
    allowed: &[PathBuf],
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
    if !allowed.iter().any(|base| resolved.starts_with(base)) {
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
            visit(root, &entry, allowed, seen, files)?;
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
    snapshot_with_library(root, paths, false)
}

/// NuttX links custom-board headers from the separately owned board copy.
/// Admit only that fixed sibling; never a source checkout or arbitrary config path.
pub fn native_headers(root: &Path, config: &str) -> Result<Value> {
    let root = root.canonicalize().map_err(|error| error.to_string())?;
    let mut allowed = vec![root.clone()];
    if config
        .lines()
        .any(|line| line == "CONFIG_ARCH_BOARD_CUSTOM=y")
    {
        for required in [
            "CONFIG_ARCH_BOARD_CUSTOM_DIR=\"../board\"",
            "CONFIG_ARCH_BOARD_CUSTOM_DIR_RELPATH=y",
        ] {
            if !config.lines().any(|line| line == required) {
                return Err("custom board headers require the owned ../board directory".into());
            }
        }
        let board = root.parent().ok_or("native output parent")?.join("board");
        let metadata = fs::symlink_metadata(&board).map_err(|error| error.to_string())?;
        if !metadata.is_dir() || metadata.is_symlink() {
            return Err("custom board must be an owned directory, not a symlink".into());
        }
        let board = board.canonicalize().map_err(|error| error.to_string())?;
        let receipt = read_json(&root.parent().unwrap().join("board-source.json"))?;
        let name = receipt["board"]
            .as_str()
            .ok_or("missing staged board identity")?;
        let selection = format!("CONFIG_ARCH_BOARD_CUSTOM_NAME=\"{name}\"");
        if receipt["schema"] != 1 || !config.lines().any(|line| line == selection) {
            return Err("custom board headers differ from the staged board identity".into());
        }
        let sources = receipt["files"]
            .as_object()
            .ok_or("missing board source hashes")?;
        if !sources.contains_key("include/board.h") {
            return Err("staged board identity omits include/board.h".into());
        }
        for (relative, expected) in sources {
            let relative = Path::new(relative);
            if relative
                .components()
                .any(|part| !matches!(part, std::path::Component::Normal(_)))
            {
                return Err("unsafe staged board source path".into());
            }
            let path = board.join(relative);
            if path
                .symlink_metadata()
                .map_err(|error| error.to_string())?
                .is_symlink()
                || !path
                    .canonicalize()
                    .map_err(|error| error.to_string())?
                    .starts_with(&board)
                || expected.as_str() != Some(hash(&path)?.as_str())
            {
                return Err(format!("staged board source changed: {}", path.display()));
            }
        }
        allowed.push(board);
    }
    let mut files = BTreeMap::new();
    visit(
        &root,
        &root.join("include"),
        &allowed,
        &mut BTreeSet::new(),
        &mut files,
    )?;
    Ok(json!(files))
}

/// Installed toolchains may link Rust sources to a separately unpacked library.
/// Read and identify that selected source tree without treating it as owned
/// output. Retained private SDKs still use the strict `snapshot` boundary.
pub fn sdk_snapshot(root: &Path) -> Result<Value> {
    snapshot_with_library(root, &["bin", "lib"], true)
}

fn snapshot_with_library(root: &Path, paths: &[&str], linked_library: bool) -> Result<Value> {
    let root = root.canonicalize().map_err(|e| e.to_string())?;
    let mut allowed = vec![root.clone()];
    let library = root.join("lib/rustlib/src/rust/library");
    if linked_library && library.is_symlink() {
        allowed.push(library.canonicalize().map_err(|e| e.to_string())?);
    }
    let mut files = BTreeMap::new();
    let mut seen = BTreeSet::new();
    for name in paths {
        visit(&root, &root.join(name), &allowed, &mut seen, &mut files)?;
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

    #[cfg(unix)]
    #[test]
    fn custom_board_headers_require_the_matching_private_copy() {
        use std::os::unix::fs::symlink;
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let nuttx = temp.0.join("nuttx");
        let board = temp.0.join("board");
        fs::create_dir_all(nuttx.join("include")).unwrap();
        fs::create_dir_all(board.join("include")).unwrap();
        let header = board.join("include/board.h");
        fs::write(&header, "#define BOARD 1\n").unwrap();
        symlink(board.join("include"), nuttx.join("include/board")).unwrap();
        let config = "CONFIG_ARCH_BOARD_CUSTOM=y\nCONFIG_ARCH_BOARD_CUSTOM_DIR=\"../board\"\nCONFIG_ARCH_BOARD_CUSTOM_DIR_RELPATH=y\nCONFIG_ARCH_BOARD_CUSTOM_NAME=\"example\"\n";
        // A sibling by itself is not a selected board or source identity.
        assert!(native_headers(&nuttx, "CONFIG_BOARD=y\n").is_err());
        assert!(native_headers(&nuttx, config).is_err());
        let receipt = json!({"schema":1,"board":"example","files":{"include/board.h":hash(&header).unwrap()}});
        write_json(&temp.0.join("board-source.json"), &receipt).unwrap();
        assert!(native_headers(&nuttx, config).is_ok());
        assert!(native_headers(&nuttx, &config.replace("example", "different-board")).is_err());
        assert!(native_headers(&nuttx, &config.replace("../board", "../elsewhere")).is_err());
        fs::write(&header, "#define BOARD 2\n").unwrap();
        assert!(native_headers(&nuttx, config).is_err());
        fs::write(&header, "#define BOARD 1\n").unwrap();
        fs::create_dir(temp.0.join("foreign")).unwrap();
        fs::write(temp.0.join("foreign/input.h"), "foreign\n").unwrap();
        symlink(temp.0.join("foreign"), nuttx.join("include/foreign")).unwrap();
        assert!(native_headers(&nuttx, config).is_err());
        fs::remove_file(nuttx.join("include/foreign")).unwrap();
        fs::rename(&board, temp.0.join("moved-board")).unwrap();
        symlink(temp.0.join("moved-board"), &board).unwrap();
        assert!(native_headers(&nuttx, config).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn installed_sdk_source_links_are_identified_without_relaxing_owned_outputs() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let sdk = temp.0.join("installed");
        fs::create_dir_all(sdk.join("bin")).unwrap();
        fs::create_dir_all(sdk.join("lib/rustlib/src/rust")).unwrap();
        fs::write(sdk.join("bin/rustc"), "compiler").unwrap();
        let source = temp.0.join("unpacked-library");
        fs::create_dir_all(source.join("std/src")).unwrap();
        let file = source.join("std/src/lib.rs");
        fs::write(&file, "original source").unwrap();
        let link = sdk.join("lib/rustlib/src/rust/library");
        std::os::unix::fs::symlink(&source, &link).unwrap();
        let original = sdk_snapshot(&sdk).unwrap();
        assert!(snapshot(&sdk, &["bin", "lib"]).is_err());
        assert_eq!(fs::read(&file).unwrap(), b"original source");
        fs::write(&file, "changed source").unwrap();
        assert_ne!(sdk_snapshot(&sdk).unwrap(), original);
        fs::write(&file, "original source").unwrap();
        assert_eq!(sdk_snapshot(&sdk).unwrap(), original);
        let replacement = temp.0.join("replacement-library");
        fs::create_dir_all(replacement.join("std/src")).unwrap();
        fs::write(replacement.join("std/src/lib.rs"), "original source").unwrap();
        fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(&replacement, &link).unwrap();
        assert_ne!(sdk_snapshot(&sdk).unwrap(), original);
        assert!(snapshot(&sdk, &["bin", "lib"]).is_err());
    }

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
