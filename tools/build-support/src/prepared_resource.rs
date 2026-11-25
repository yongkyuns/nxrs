//! Prepared native archives for NuttX resource and HAL adapters.
use crate::{
    hash, hash_bytes, native, native_nuttx, profile::manifest, read_json, Result, TempDir,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    ffi::OsStr,
    fs,
    path::{Path, PathBuf},
};

pub const ENVIRONMENT: &str = "NXRS_NUTTX_PREPARED_RESOURCES";
pub const PACKAGE_LIST: &str = "packages.json";
const COMPILER_ENV: &[&str] = &[
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "OBJC_INCLUDE_PATH",
    "LIBRARY_PATH",
    "GCC_EXEC_PREFIX",
    "COMPILER_PATH",
    "SOURCE_DATE_EPOCH",
    "SDKROOT",
];

pub fn selected(
    directory: &Path,
    profile: &crate::profile::Profile,
    features: &BTreeSet<String>,
) -> Result<bool> {
    let manifest = manifest(&directory.join("Cargo.toml"))?;
    let package = &manifest["package"];
    match package["metadata"]["nxrs"]["kind"].as_str() {
        Some("native-resource") => {}
        Some("hal-capability") => {
            let name = package["name"].as_str().ok_or("missing HAL package name")?;
            if profile.binding(crate::capability_name(name)?) != "nuttx" {
                return Ok(false);
            }
        }
        _ => return Ok(false),
    }
    let source = directory.join("src/platform/nuttx/ffi");
    let mut paths = Vec::new();
    native::tree(&source, &mut paths)?;
    Ok(!native::sources(&source, &paths).is_empty()
        && native::required_features(&manifest, "nuttx")?
            .iter()
            .all(|feature| features.contains(feature)))
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    identity: String,
    tool_path: String,
    inputs: BTreeMap<PathBuf, String>,
    archive: String,
}

fn digest(value: &impl Serialize) -> Result<String> {
    Ok(hash_bytes(
        &serde_json::to_vec(value).map_err(|e| e.to_string())?,
    ))
}

fn input(path: &Path) -> Result<String> {
    if !path.is_dir() {
        return hash(path);
    }
    let mut paths = Vec::new();
    native::tree(path, &mut paths)?;
    let entries = paths
        .iter()
        .map(|entry| {
            let relative = entry.strip_prefix(path).map_err(|e| e.to_string())?;
            let value = if entry.is_dir() {
                "directory".into()
            } else {
                hash(entry)?
            };
            Ok((relative, value))
        })
        .collect::<Result<BTreeMap<_, _>>>()?;
    digest(&entries)
}

struct Resource {
    directory: PathBuf,
    source: PathBuf,
    context: PathBuf,
    data: serde_json::Value,
    cache: PathBuf,
    suffix: String,
    identity: String,
    tools: [PathBuf; 2],
}

impl Resource {
    fn new(directory: &Path, target: &str, context: &Path, output: &Path) -> Result<Self> {
        let directory = directory.canonicalize().map_err(|e| e.to_string())?;
        let context = context.canonicalize().map_err(|e| e.to_string())?;
        if !output.is_absolute() {
            return Err("prepared native resource directory must be absolute".into());
        }
        let data = native_nuttx::read_context(&context, target)?;
        let tool = |key: &str| -> Result<PathBuf> {
            let words = native_nuttx::words(&data, key)?;
            let path = PathBuf::from(words.first().ok_or("missing native resource tool")?);
            if !path.is_absolute() {
                return Err("prepared native resource tools require absolute paths".into());
            }
            path.canonicalize().map_err(|error| error.to_string())
        };
        let package = manifest(&directory.join("Cargo.toml"))?;
        let suffix = native::suffix(
            package["package"]["name"]
                .as_str()
                .ok_or("missing resource package name")?,
        )?;
        let environment: BTreeMap<_, _> = COMPILER_ENV
            .iter()
            .filter_map(|name| {
                env::var_os(name).map(|value| (*name, hash_bytes(value.as_encoded_bytes())))
            })
            .collect();
        let generator = hash_bytes(
            concat!(
                env!("CARGO_PKG_VERSION"),
                include_str!("prepared_resource.rs"),
                include_str!("native.rs"),
                include_str!("native_nuttx.rs"),
            )
            .as_bytes(),
        );
        let identity = digest(&(target, &context, environment, generator))?;
        let cache = output.join(digest(&directory)?);
        for path in [output, cache.as_path()] {
            if path.symlink_metadata().is_ok_and(|info| info.is_symlink()) {
                return Err("prepared native resource output must not be a symlink".into());
            }
        }
        let tools = [tool("cc")?, tool("ar")?];
        Ok(Self {
            source: directory.join("src/platform/nuttx/ffi"),
            directory,
            context,
            data,
            cache,
            suffix,
            identity,
            tools,
        })
    }

    fn required(&self) -> Result<BTreeMap<PathBuf, String>> {
        [
            self.directory.join("Cargo.toml"),
            self.context.clone(),
            self.source.clone(),
            self.tools[0].clone(),
            self.tools[1].clone(),
        ]
        .into_iter()
        .map(|path| Ok((path.clone(), input(&path)?)))
        .collect()
    }

    fn filename(&self) -> String {
        format!("lib{}_native.a", self.suffix)
    }

    fn archive_path(&self, sha256: &str) -> Result<PathBuf> {
        let bucket = self.cache.join(sha256);
        let archive = bucket.join(self.filename());
        if [bucket, archive.clone()]
            .iter()
            .any(|path| path.symlink_metadata().is_ok_and(|info| info.is_symlink()))
        {
            return Err("prepared native resource archive must not be a symlink".into());
        }
        Ok(archive)
    }

    fn checked(&self, required: &BTreeMap<PathBuf, String>) -> Result<(Record, PathBuf)> {
        let record: Record = serde_json::from_value(read_json(&self.cache.join("inputs.json"))?)
            .map_err(|error| error.to_string())?;
        if record.identity != self.identity
            || !required
                .iter()
                .all(|(path, value)| record.inputs.get(path) == Some(value))
            || !record.inputs.iter().all(|(path, value)| {
                required.contains_key(path) || input(path).ok().as_ref() == Some(value)
            })
            || record.archive.len() != 64
            || !record.archive.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err("stale native resource inputs".into());
        }
        let archive = self.archive_path(&record.archive)?;
        if hash(&archive)? != record.archive {
            return Err("prepared native resource archive changed".into());
        }
        Ok((record, archive))
    }
}

/// Reuse a current archive or compile it with the controller's NuttX context.
pub fn prepare(
    directory: &Path,
    target: &str,
    context: &Path,
    output: &Path,
    compiler_path: &OsStr,
) -> Result<PathBuf> {
    let resource = Resource::new(directory, target, context, output)?;
    let mut inputs = resource.required()?;
    let mut paths = Vec::new();
    native::tree(&resource.source, &mut paths)?;
    paths.sort();
    let sources = native::sources(&resource.source, &paths);
    if sources.is_empty() {
        return Err("native resource has no helper sources".into());
    }
    // A newly shadowing header changes the compiler's dependency set even
    // when every previously used file and all of the flags are unchanged.
    let dependencies =
        native_nuttx::scan_dependencies(&resource.source, &sources, &resource.data, compiler_path)?;
    for path in dependencies.iter().flatten() {
        let canonical = path.canonicalize().map_err(|error| error.to_string())?;
        for path in [path, &canonical] {
            let value = hash(path)?;
            if inputs.get(path).is_some_and(|previous| previous != &value) {
                return Err("native resource inputs changed during dependency discovery".into());
            }
            inputs.insert(path.clone(), value);
        }
    }
    let tool_path = hash_bytes(compiler_path.as_encoded_bytes());
    if let Ok((record, archive)) = resource.checked(&inputs) {
        if record.tool_path == tool_path && record.inputs == inputs {
            return Ok(archive);
        }
    }
    fs::create_dir_all(&resource.cache).map_err(|e| e.to_string())?;
    for entry in fs::read_dir(&resource.cache).map_err(|e| e.to_string())? {
        if entry
            .map_err(|e| e.to_string())?
            .file_type()
            .map_err(|e| e.to_string())?
            .is_symlink()
        {
            return Err("prepared native resource archive must not be a symlink".into());
        }
    }
    let temporary = TempDir::new(&resource.cache)?;
    native_nuttx::compile(
        &resource.source,
        &resource.suffix,
        target,
        &temporary.0,
        &sources,
        (
            &resource.context,
            Some(native_nuttx::Preparation {
                compiler_path,
                dependencies: &dependencies,
            }),
        ),
        Vec::new(),
    )?;
    for (path, digest) in &inputs {
        if input(path)? != *digest {
            return Err("native resource inputs changed during compilation".into());
        }
    }
    let compiled = temporary.0.join(resource.filename());
    let sha256 = hash(&compiled)?;
    let archive = resource.archive_path(&sha256)?;
    fs::create_dir_all(archive.parent().unwrap()).map_err(|e| e.to_string())?;
    if hash(&archive).ok().as_ref() != Some(&sha256) {
        fs::rename(&compiled, &archive).map_err(|e| e.to_string())?;
    }
    let record = serde_json::to_value(Record {
        identity: resource.identity,
        tool_path,
        inputs,
        archive: sha256,
    })
    .map_err(|e| e.to_string())?;
    let candidate = temporary.0.join("inputs.json");
    crate::write_json(&candidate, &record)?;
    fs::rename(candidate, resource.cache.join("inputs.json")).map_err(|e| e.to_string())?;
    Ok(archive)
}

/// Validate the controller's inputs before exposing the archive to Cargo.
pub(crate) fn consume(directory: &Path, target: &str, output: &Path) -> Result<Vec<String>> {
    let context = PathBuf::from(
        env::var_os("NXRS_NUTTX_NATIVE_CONFIG").ok_or("missing NuttX native context")?,
    );
    let resource = Resource::new(directory, target, &context, output)?;
    let (record, archive) = resource
        .checked(&resource.required()?)
        .map_err(|error| format!("{error}; rerun cargo nxrs build to prepare native resources"))?;
    let mut directives = vec![
        "cargo:rerun-if-env-changed=NXRS_NUTTX_NATIVE_CONFIG".into(),
        format!("cargo:rerun-if-changed={}", context.display()),
        format!("cargo:rerun-if-changed={}", resource.cache.display()),
        format!(
            "cargo:rerun-if-changed={}",
            resource.cache.join("inputs.json").display()
        ),
        format!("cargo:rerun-if-changed={}", archive.display()),
        format!(
            "cargo:rustc-link-search=native={}",
            archive.parent().unwrap().display()
        ),
        format!("cargo:rustc-link-lib=static={}_native", resource.suffix),
    ];
    directives.extend(
        COMPILER_ENV
            .iter()
            .map(|name| format!("cargo:rerun-if-env-changed={name}")),
    );
    directives.extend(
        record
            .inputs
            .keys()
            .map(|path| format!("cargo:rerun-if-changed={}", path.display())),
    );
    Ok(directives)
}
