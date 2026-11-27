//! Resolve optional build companions without guessing a board from addresses.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::{Error, Result};

#[path = "layout_catalog.rs"]
mod catalog;
pub use catalog::SUPPORTED_LAYOUT_BOARDS;

pub(crate) struct BuiltinLayout {
    pub id: &'static str,
    pub label: &'static str,
    pub nuttx_board: &'static str,
    pub nuttx_revision: &'static str,
    pub required_config: &'static [(&'static str, &'static str)],
    pub json: &'static str,
}

/// Board declarations from the same completed build as the selected ELF.
/// This is a producer's build receipt, not attestation of a running device.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BuildContext {
    pub board: String,
    pub nuttx_revision: String,
    pub config_path: PathBuf,
    pub receipt_path: PathBuf,
    pub elf_sha256: String,
    pub config: BTreeMap<String, String>,
}

pub(crate) struct DiscoveredInputs {
    pub map: Option<PathBuf>,
    pub layout: Option<String>,
    pub warnings: Vec<String>,
    pub explicit_layout: bool,
}

fn error(message: impl Into<String>) -> Error {
    Error(format!("Build discovery: {}", message.into()))
}

fn text(path: &Path, limit: u64) -> Result<String> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > limit {
        return Err(error(format!(
            "{} must be a regular file containing 1..{limit} bytes",
            path.display()
        )));
    }
    let mut value = String::new();
    fs::File::open(path)?
        .take(limit + 1)
        .read_to_string(&mut value)?;
    if value.len() as u64 > limit {
        return Err(error(format!(
            "{} grew beyond its input bound",
            path.display()
        )));
    }
    Ok(value)
}

pub(crate) fn read_map_text(path: &Path) -> Result<String> {
    text(path, 64 * 1024 * 1024)
}

pub(crate) fn read_layout_text(path: &Path) -> Result<String> {
    text(path, 8 * 1024 * 1024)
}

fn valid_sha(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn config_values(value: &str) -> Result<BTreeMap<String, String>> {
    let mut result = BTreeMap::new();
    for line in value.lines() {
        let pair = if line.starts_with("CONFIG_") {
            line.split_once('=')
        } else {
            line.strip_prefix("# ")
                .and_then(|line| line.strip_suffix(" is not set"))
                .map(|key| (key, "n"))
        };
        if let Some((key, value)) = pair {
            if result.insert(key.to_owned(), value.to_owned()).is_some() {
                return Err(error(format!(
                    "duplicate effective configuration key {key}"
                )));
            }
        }
    }
    Ok(result)
}

fn receipt_hash(receipt: &str, relative: &Path) -> Result<String> {
    let relative = relative.to_string_lossy().replace('\\', "/");
    let suffix = format!("/{relative}");
    let mut values = BTreeSet::new();
    for line in receipt.lines() {
        let Some((hash, path)) = line.split_once("  ") else {
            continue;
        };
        let path = path.replace('\\', "/");
        if path == relative || path.ends_with(&suffix) {
            if !valid_sha(hash) {
                return Err(error(format!(
                    "invalid SHA-256 for {relative} in binaries.sha256"
                )));
            }
            values.insert(hash.to_ascii_lowercase());
        }
    }
    if values.len() != 1 {
        return Err(error(format!(
            "binaries.sha256 has no unambiguous identity for {relative}"
        )));
    }
    Ok(values.into_iter().next().expect("one hash"))
}

fn roots(elf: &Path) -> Vec<&Path> {
    let parent = elf
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let mut roots = vec![parent];
    // cargo nxrs build retains the final ELF under <output>/nuttx/nuttx.
    // Do not search arbitrary ancestors or other build directories.
    if parent.file_name().is_some_and(|name| name == "nuttx") {
        if let Some(root) = parent.parent() {
            roots.push(root);
        }
    }
    roots
}

fn context_at(root: &Path, elf: &Path, elf_sha256: &str) -> Result<Option<BuildContext>> {
    let provenance = root.join("provenance.txt");
    let hashes = root.join("binaries.sha256");
    let config_path = root.join("resolved.config");
    if provenance.exists() || hashes.exists() {
        let receipt = text(&hashes, 8 * 1024 * 1024)?;
        let relative = if root == Path::new(".") {
            elf
        } else {
            elf.strip_prefix(root)
                .map_err(|_| error("ELF is outside the build receipt directory"))?
        };
        if receipt_hash(&receipt, relative)? != elf_sha256 {
            return Err(error(
                "binaries.sha256 belongs to a different ELF (SHA-256 mismatch)",
            ));
        }
        let config_text = text(&config_path, 8 * 1024 * 1024)?;
        if receipt_hash(&receipt, Path::new("resolved.config"))? != digest(config_text.as_bytes()) {
            return Err(error("resolved.config does not match binaries.sha256"));
        }
        let provenance_text = text(&provenance, 8 * 1024 * 1024)?;
        let mut fields = BTreeMap::new();
        for line in provenance_text.lines() {
            if let Some((key @ ("board" | "nuttx"), value)) = line.split_once('=') {
                if fields.insert(key, value).is_some() {
                    return Err(error(format!("duplicate {key} in provenance.txt")));
                }
            }
        }
        let board = fields
            .get("board")
            .ok_or_else(|| error("provenance.txt has no board declaration"))?;
        let revision = fields
            .get("nuttx")
            .ok_or_else(|| error("provenance.txt has no NuttX revision"))?;
        if board.is_empty()
            || revision.len() != 40
            || !revision.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(error("invalid board or NuttX revision in provenance.txt"));
        }
        return Ok(Some(BuildContext {
            board: (*board).to_owned(),
            nuttx_revision: revision.to_ascii_lowercase(),
            config_path,
            receipt_path: hashes,
            elf_sha256: elf_sha256.to_owned(),
            config: config_values(&config_text)?,
        }));
    }
    let build_path = root.join("build.json");
    if !build_path.exists() {
        return Ok(None);
    }
    let build: Value = serde_json::from_str(&text(&build_path, 8 * 1024 * 1024)?)?;
    let workload = build["workload"].as_str().unwrap_or_default();
    if !matches!(
        workload,
        "native NuttX memory_timing qualification fixture"
            | "native isolated heap replay qualification"
    ) {
        return Ok(None);
    }
    if build["schema_version"] != 1
        || !build["elf_sha256"]
            .as_str()
            .is_some_and(|hash| hash.eq_ignore_ascii_case(elf_sha256))
    {
        return Err(error(
            "fixture build.json belongs to a different ELF (SHA-256 mismatch)",
        ));
    }
    let config_text = text(&config_path, 8 * 1024 * 1024)?;
    if !build["config_sha256"]
        .as_str()
        .is_some_and(|hash| hash.eq_ignore_ascii_case(&digest(config_text.as_bytes())))
    {
        return Err(error("resolved.config does not match fixture build.json"));
    }
    let revision = build["nuttx_revision"]
        .as_str()
        .ok_or_else(|| error("fixture build.json has no NuttX revision"))?;
    Ok(Some(BuildContext {
        board: "mps2-an521:nsh".to_owned(),
        nuttx_revision: revision.to_ascii_lowercase(),
        config_path,
        receipt_path: build_path,
        elf_sha256: elf_sha256.to_owned(),
        config: config_values(&config_text)?,
    }))
}

pub fn discover_build_context(elf: &Path) -> Result<Option<BuildContext>> {
    let limit = 512 * 1024 * 1024;
    let metadata = fs::symlink_metadata(elf)?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > limit {
        return Err(error("ELF must be a regular file containing 1..512 MiB"));
    }
    let file = fs::File::open(elf)?;
    if !file.metadata()?.is_file() || file.metadata()?.len() != metadata.len() {
        return Err(error("ELF changed while opening"));
    }
    let mut input = file.take(limit + 1);
    let mut hash = Sha256::new();
    let mut bytes = 0;
    let mut buffer = [0; 64 * 1024];
    loop {
        let read = input.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        bytes += read as u64;
        if bytes > limit {
            return Err(error("ELF grew beyond 512 MiB"));
        }
        hash.update(&buffer[..read]);
    }
    if bytes != metadata.len() || input.get_ref().metadata()?.modified()? != metadata.modified()? {
        return Err(error("ELF changed while hashing"));
    }
    discover_context(elf, &format!("{:x}", hash.finalize()))
}

fn discover_context(elf: &Path, elf_sha256: &str) -> Result<Option<BuildContext>> {
    let mut contexts = Vec::new();
    for root in roots(elf) {
        if let Some(context) = context_at(root, elf, elf_sha256)? {
            contexts.push(context);
        }
    }
    if contexts.len() > 1 {
        return Err(error(
            "multiple build receipts match this ELF; supply an explicit layout",
        ));
    }
    Ok(contexts.pop())
}

/// A layout may be portable across builds; when it carries a build identity,
/// enforce that identity even when selected explicitly.
pub(crate) fn checked_layout(
    value: &str,
    elf_sha256: &str,
    require_identity: bool,
) -> Result<String> {
    let json: Value = serde_json::from_str(value)?;
    if let Some(build) = json.get("build") {
        if !build
            .get("elf_sha256")
            .and_then(Value::as_str)
            .is_some_and(|hash| hash.eq_ignore_ascii_case(elf_sha256))
        {
            return Err(error(
                "layout belongs to a different ELF (SHA-256 mismatch)",
            ));
        }
    } else if require_identity {
        return Err(error("adjacent layout has no build.elf_sha256; choose it explicitly to apply a portable board descriptor"));
    }
    let parsed = crate::parse_layout(value)?;
    if parsed.regions.iter().any(|region| region.id == "elf-file") {
        return Err(error("layout uses reserved region id elf-file"));
    }
    Ok(value.to_owned())
}

fn candidate_files(paths: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut seen = BTreeSet::new();
    paths
        .into_iter()
        .filter(|path| seen.insert(path.clone()) && path.exists())
        .collect()
}

fn configured_size(value: &str) -> Option<u64> {
    let value = value.trim().trim_matches('"');
    if let Some(hex) = value
        .strip_prefix("0x")
        .or_else(|| value.strip_prefix("0X"))
    {
        u64::from_str_radix(hex, 16).ok()
    } else {
        value.parse().ok()
    }
}

fn specialize(builtin: &BuiltinLayout, context: Option<&BuildContext>) -> Result<String> {
    let Some(context) = context else {
        return Ok(builtin.json.to_owned());
    };
    let mut json: Value = serde_json::from_str(builtin.json)?;
    json["build"] = serde_json::json!({"elf_sha256":context.elf_sha256});
    let rules = json["configured_resources"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    for rule in rules {
        if rule["enabled_by"]
            .as_str()
            .is_some_and(|key| context.config.get(key).is_none_or(|value| value != "y"))
        {
            continue;
        }
        let size = if let Some(key) = rule["size_key"].as_str() {
            context
                .config
                .get(key)
                .and_then(|value| configured_size(value))
                .filter(|size| {
                    rule["allowed_sizes"].as_array().is_some_and(|allowed| {
                        allowed.iter().any(|value| value.as_u64() == Some(*size))
                    })
                })
        } else {
            let choices: Vec<_> = rule["size_choices"]
                .as_object()
                .into_iter()
                .flat_map(|choices| choices.iter())
                .filter(|(key, _)| context.config.get(*key).is_some_and(|value| value == "y"))
                .filter_map(|(_, size)| size.as_u64())
                .collect();
            if choices.len() == 1 {
                Some(choices[0])
            } else {
                None
            }
        };
        let id = rule["id"]
            .as_str()
            .ok_or_else(|| error("bundled configured resource has no id"))?;
        let name = rule["name"]
            .as_str()
            .ok_or_else(|| error("bundled configured resource has no name"))?;
        let warning = if let Some(size) = size.filter(|size| *size > 0) {
            let resources = json
                .get_mut("physical_resources")
                .and_then(Value::as_array_mut)
                .ok_or_else(|| error("bundled layout has no physical_resources array"))?;
            if resources.iter().any(|resource| resource["id"] == id) {
                return Err(error(format!("duplicate bundled resource {id}")));
            }
            resources.push(serde_json::json!({"id":id,"name":name,"size":size}));
            format!("{name}: {size} bytes are declared by the identity-matched effective build configuration. No CPU-to-device mapping or measured capacity is inferred; the resource remains unmapped.")
        } else {
            format!("{name}: the effective build configuration does not establish one supported capacity; its capacity remains unknown.")
        };
        json.get_mut("warnings")
            .and_then(Value::as_array_mut)
            .ok_or_else(|| error("bundled layout has no warnings array"))?
            .push(Value::String(warning));
    }
    Ok(serde_json::to_string(&json)?)
}

pub(crate) fn discover(
    elf: &Path,
    map: Option<&Path>,
    layout: Option<&Path>,
    board: Option<&str>,
    elf_sha256: &str,
) -> Result<DiscoveredInputs> {
    if layout.is_some() && board.is_some() {
        return Err(error(
            "choose either an explicit layout or a board, not both",
        ));
    }
    let mut result = DiscoveredInputs {
        map: map.map(Path::to_path_buf),
        layout: None,
        warnings: Vec::new(),
        explicit_layout: layout.is_some() || board.is_some(),
    };
    if result.map.is_none() {
        let appended = PathBuf::from(format!("{}.map", elf.display()));
        let candidates = candidate_files(vec![elf.with_extension("map"), appended]);
        if candidates.len() == 1 {
            // The importer checks output sections against the ELF. A sibling
            // map has no build hash of its own and is labelled accordingly.
            match text(&candidates[0], 64 * 1024 * 1024) {
                Ok(_) => {
                    result.warnings.push(format!("Automatically selected adjacent linker map {}. Its output-section addresses and sizes are checked against the ELF; this is not a cryptographic map identity.", candidates[0].display()));
                    result.map = candidates.into_iter().next();
                }
                Err(reason) => result.warnings.push(format!(
                    "Skipped automatic linker map {}: {reason}. ELF placement remains available.",
                    candidates[0].display()
                )),
            }
        } else if candidates.len() > 1 {
            result.warnings.push("Multiple adjacent linker maps were found; choose the map explicitly. ELF placement remains available.".into());
        }
    }
    if let Some(path) = layout {
        result.layout = Some(checked_layout(
            &text(path, 8 * 1024 * 1024)?,
            elf_sha256,
            false,
        )?);
        result.warnings.push(format!(
            "Memory layout selected explicitly: {}.",
            path.display()
        ));
        return Ok(result);
    }
    if let Some(id) = board {
        let builtin = catalog::BUILTIN_LAYOUTS
            .iter()
            .find(|entry| entry.id == id)
            .ok_or_else(|| {
                error(format!(
                    "unsupported board {id:?}; choose a listed board or supply a layout"
                ))
            })?;
        let context = discover_context(elf, elf_sha256)
            .ok()
            .flatten()
            .filter(|context| {
                context.board == builtin.nuttx_board
                    && context.nuttx_revision == builtin.nuttx_revision
                    && builtin.required_config.iter().all(|(key, value)| {
                        context
                            .config
                            .get(*key)
                            .is_some_and(|actual| actual == value)
                    })
            });
        result.layout = Some(specialize(builtin, context.as_ref())?);
        result.warnings.push(format!("Explicit board selection: {}. This applies the bundled reference topology; selecting it does not verify target configuration or hardware.", builtin.label));
        return Ok(result);
    }
    let candidates = candidate_files(vec![
        elf.with_extension("layout.json"),
        elf.with_file_name("layout.json"),
    ]);
    let mut layouts = Vec::new();
    for path in candidates {
        match text(&path, 8 * 1024 * 1024)
            .and_then(|value| checked_layout(&value, elf_sha256, true))
        {
            Ok(value) => layouts.push((path, value)),
            Err(reason) => result.warnings.push(format!(
                "Skipped automatic layout {}: {reason}",
                path.display()
            )),
        }
    }
    if layouts.len() == 1 {
        let (path, value) = layouts.pop().expect("one layout");
        result.layout = Some(value);
        result.warnings.push(format!(
            "Automatically selected layout {} after verifying its ELF SHA-256.",
            path.display()
        ));
        return Ok(result);
    }
    if layouts.len() > 1 {
        result.warnings.push("Multiple identity-matched adjacent layouts were found; choose one explicitly. No physical layout was applied.".into());
        return Ok(result);
    }
    match discover_context(elf, elf_sha256) {
        Ok(Some(context)) => {
            let matches: Vec<_> = catalog::BUILTIN_LAYOUTS.iter().filter(|entry| {
                entry.nuttx_board == context.board && entry.nuttx_revision == context.nuttx_revision &&
                    entry.required_config.iter().all(|(key, value)| context.config.get(*key).is_some_and(|actual| actual == value))
            }).collect();
            if let [builtin] = matches.as_slice() {
                result.layout = Some(specialize(builtin, Some(&context))?);
                result.warnings.push(format!("Automatically selected {} from {} after matching the ELF, effective configuration, declared board and pinned NuttX revision. This is the bundled declared board topology, not an observation of the running target or emulator version.", builtin.label, context.receipt_path.display()));
            } else {
                result.warnings.push(format!("Build receipt identifies {} at NuttX {}, but no unique bundled physical layout matches its configuration. The overview uses linker regions or ELF placement.", context.board, context.nuttx_revision));
            }
        }
        Ok(None) => result.warnings.push("No identity-bound board metadata was found. The overview uses linker regions or ELF placement; select a bundled board only when its configuration is known.".into()),
        Err(reason) => result.warnings.push(format!("Automatic board layout was not applied: {reason}. The overview uses linker regions or ELF placement.")),
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn configured_external_memory_remains_unmapped_and_unknown_sizes_are_not_filled() {
        let builtin = catalog::BUILTIN_LAYOUTS
            .iter()
            .find(|entry| entry.id == "esp32s3-cores3")
            .unwrap();
        let mut context = BuildContext {
            board: builtin.nuttx_board.into(),
            nuttx_revision: builtin.nuttx_revision.into(),
            config_path: PathBuf::from("resolved.config"),
            receipt_path: PathBuf::from("binaries.sha256"),
            elf_sha256: "a".repeat(64),
            config: BTreeMap::from([
                ("CONFIG_ESP32S3_SPIRAM".into(), "y".into()),
                ("CONFIG_ESP32S3_SPIRAM_SIZE".into(), "0x800000".into()),
                ("CONFIG_ESP32S3_FLASH_16M".into(), "y".into()),
            ]),
        };
        let layout = crate::parse_layout(&specialize(builtin, Some(&context)).unwrap()).unwrap();
        assert_eq!(
            layout
                .physical_resources
                .iter()
                .find(|resource| resource.id == "configured-psram")
                .unwrap()
                .size,
            8 * 1024 * 1024
        );
        assert_eq!(
            layout
                .physical_resources
                .iter()
                .find(|resource| resource.id == "configured-flash")
                .unwrap()
                .size,
            16 * 1024 * 1024
        );
        assert!(!layout
            .regions
            .iter()
            .filter_map(|region| region.physical.as_ref())
            .any(|mapping| mapping.id.starts_with("configured-")));
        context
            .config
            .insert("CONFIG_ESP32S3_SPIRAM_SIZE".into(), "-1".into());
        context
            .config
            .insert("CONFIG_ESP32S3_FLASH_4M".into(), "y".into());
        let unknown = crate::parse_layout(&specialize(builtin, Some(&context)).unwrap()).unwrap();
        assert!(!unknown
            .physical_resources
            .iter()
            .any(|resource| resource.id.starts_with("configured-")));
        assert_eq!(
            unknown
                .warnings
                .iter()
                .filter(|warning| warning.contains("capacity remains unknown"))
                .count(),
            2
        );
    }
}
