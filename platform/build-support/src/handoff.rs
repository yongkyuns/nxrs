//! Controller-owned preparation with a verified, package-scoped Cargo transport.
//!
//! The controller renders configuration and compiles native helpers before Cargo.
//! Generic HAL adapters only validate and deliver an explicitly selected receipt.
//! The legacy adapter remains available when no preparation receipt is selected.
use crate::{bindings, hash, hash_bytes, native, preparation, profile::Profile, Result, TempDir};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    process::Command,
};

const VERSION: u32 = 1;
pub const ENVIRONMENT: &str = "NXRS_PREPARED_BUILD";

/// A package identity and target feature set verified through the real Cargo graph.
#[derive(Debug, Clone)]
pub struct Package {
    pub identity: String,
    pub directory: PathBuf,
    pub features: BTreeSet<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct File {
    path: PathBuf,
    sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Input {
    path: PathBuf,
    kind: String,
    sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Identity {
    version: u32,
    generator: String,
    profile: File,
    package: String,
    cargo_identity: String,
    directory: PathBuf,
    cargo_features: BTreeSet<String>,
    features: BTreeSet<String>,
    provider: String,
    target: String,
    os: String,
    native: native::Inputs,
    inputs: Vec<Input>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Prepared {
    identity: Identity,
    /// Includes compiler-discovered headers outside declared provider directories.
    inputs: Vec<Input>,
    outputs: Vec<File>,
    directives: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Reference {
    package: String,
    directory: PathBuf,
    record: File,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    version: u32,
    generator: String,
    profile: File,
    target: String,
    os: String,
    packages: Vec<Reference>,
}

/// Fingerprint the shared generator implementation, including validation and
/// native preparation. These are this package's own sources, never sibling HAL
/// catalogs. Both controller and adapter must use the same implementation.
fn generator() -> String {
    hash_bytes(
        concat!(
            env!("CARGO_PKG_VERSION"),
            include_str!("lib.rs"),
            include_str!("handoff.rs"),
            include_str!("bindings.rs"),
            include_str!("composition.rs"),
            include_str!("configuration.rs"),
            include_str!("preparation.rs"),
            include_str!("profile.rs"),
            include_str!("overlay.rs"),
            include_str!("resolved.rs"),
            include_str!("native.rs"),
            include_str!("native_nuttx.rs"),
        )
        .as_bytes(),
    )
}

fn path_text(path: &Path) -> Result<&str> {
    path.to_str()
        .filter(|text| !text.contains(['\r', '\n']))
        .ok_or_else(|| {
            format!(
                "preparation requires a UTF-8 path without line breaks: {}",
                path.display()
            )
        })
}

fn absolute(path: &Path) -> Result<PathBuf> {
    let path = std::path::absolute(path).map_err(|error| error.to_string())?;
    path_text(&path)?;
    Ok(path)
}

fn file(path: &Path) -> Result<File> {
    let path = absolute(path)?;
    Ok(File {
        sha256: hash(&path)?,
        path,
    })
}

fn bytes(value: &impl Serialize) -> Result<Vec<u8>> {
    let mut value = serde_json::to_vec_pretty(value).map_err(|error| error.to_string())?;
    value.push(b'\n');
    Ok(value)
}

fn read<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T> {
    serde_json::from_slice(&fs::read(path).map_err(|error| format!("{}: {error}", path.display()))?)
        .map_err(|error| format!("{}: {error}", path.display()))
}

fn write(path: &Path, contents: &[u8]) -> Result<()> {
    if fs::read(path).ok().as_deref() == Some(contents) {
        return Ok(());
    }
    let parent = path
        .parent()
        .ok_or("preparation output requires a parent")?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let temporary = TempDir::new(parent)?;
    let fresh = temporary.0.join("output");
    fs::write(&fresh, contents).map_err(|error| error.to_string())?;
    fs::rename(fresh, path).map_err(|error| format!("{}: {error}", path.display()))
}

fn snapshot(paths: impl IntoIterator<Item = PathBuf>) -> Result<Vec<Input>> {
    let mut inputs = BTreeMap::new();
    for path in paths {
        let path = absolute(&path)?;
        let metadata = fs::metadata(&path)
            .map_err(|error| format!("preparation input {}: {error}", path.display()))?;
        let (kind, sha256) = if metadata.is_file() {
            ("file", hash(&path)?)
        } else if metadata.is_dir() {
            // Native discovery enumerates watched trees. Directory entries detect
            // additions/removals; every discovered source/header has its own hash.
            let mut entries = BTreeMap::new();
            for entry in fs::read_dir(&path).map_err(|error| error.to_string())? {
                let entry = entry.map_err(|error| error.to_string())?;
                let name = entry
                    .file_name()
                    .into_string()
                    .map_err(|_| "non-UTF-8 preparation input")?;
                let kind = entry.file_type().map_err(|error| error.to_string())?;
                let value = if kind.is_symlink() {
                    format!(
                        "symlink:{}",
                        path_text(
                            &fs::read_link(entry.path()).map_err(|error| error.to_string())?
                        )?
                    )
                } else if kind.is_dir() {
                    "directory".into()
                } else {
                    "file".into()
                };
                entries.insert(name, value);
            }
            ("directory", hash_bytes(&bytes(&entries)?))
        } else {
            return Err(format!("unsupported preparation input {}", path.display()));
        };
        inputs.insert(
            path.clone(),
            Input {
                path,
                kind: kind.into(),
                sha256,
            },
        );
    }
    Ok(inputs.into_values().collect())
}

fn identity(
    package: &Package,
    profile: &Profile,
    receipt: &Path,
    target: &str,
    os: &str,
    context: &native::Context,
) -> Result<Identity> {
    let declarations = bindings::Package::load(&package.directory)?;
    let directory = declarations
        .manifest
        .path
        .parent()
        .ok_or("missing package directory")?
        .to_path_buf();
    let capability = declarations
        .name
        .strip_prefix("nxrs-")
        .ok_or("HAL package name")?;
    if let Some(expected) = profile.catalogs().package(capability) {
        if !declarations.same_declarations(expected) {
            return Err(format!(
                "binding declarations for {} differ from the resolved profile",
                declarations.name
            ));
        }
    }
    let provider = profile.binding(capability);
    let native = if ["linux", "macos", "nuttx"].contains(&provider) {
        native::inputs(&directory, os, target, context)?
    } else {
        native::Inputs {
            files: Vec::new(),
            environment: BTreeMap::new(),
            identity: serde_json::Value::Null,
        }
    };
    let paths = declarations
        .inputs()
        .map(|input| input.path.clone())
        .chain(profile.source_paths().cloned())
        .chain(native.files.iter().cloned());
    Ok(Identity {
        version: VERSION,
        generator: generator(),
        profile: file(receipt)?,
        package: declarations.name.clone(),
        cargo_identity: package.identity.clone(),
        directory,
        cargo_features: package.features.clone(),
        features: context.feature_environment().clone(),
        provider: provider.into(),
        target: target.into(),
        os: os.into(),
        inputs: snapshot(paths)?,
        native,
    })
}

fn unchanged(inputs: &[Input]) -> Result<bool> {
    for input in inputs {
        if !input.path.try_exists().map_err(|error| error.to_string())? {
            // A compiler-discovered header can disappear while a later include
            // path supplies its replacement. Let the controller compile again.
            return Ok(false);
        }
    }
    Ok(snapshot(inputs.iter().map(|input| input.path.clone()))? == inputs)
}

fn verify_outputs(prepared: &Prepared) -> Result<()> {
    for output in &prepared.outputs {
        if hash(&output.path)? != output.sha256 {
            return Err(format!("prepared output was modified: {}; remove the damaged preparation cache and prepare again", output.path.display()));
        }
    }
    Ok(())
}

fn load(reference: &Reference) -> Result<Prepared> {
    if file(&reference.record.path)? != reference.record {
        return Err("prepared package record was modified; prepare again".into());
    }
    let prepared: Prepared = read(&reference.record.path)?;
    if prepared.identity.directory != reference.directory
        || prepared.identity.package != reference.package
    {
        return Err("prepared package identity does not match its receipt".into());
    }
    Ok(prepared)
}

fn prepare_package(
    package: &Package,
    profile: &Profile,
    receipt: &Path,
    target: &str,
    os: &str,
    command: &Command,
    out: &Path,
) -> Result<Reference> {
    let context = native::Context::for_command(command, package.features.clone())?;
    let identity = identity(package, profile, receipt, target, os, &context)?;
    let index = out
        .join("index")
        .join(format!("{}.json", hash_bytes(&bytes(&identity)?)));
    if index.is_file() {
        let reference: Reference = read(&index)?;
        let prepared = load(&reference)?;
        if prepared.identity != identity {
            return Err("preparation index contains a mismatched package".into());
        }
        if unchanged(&prepared.inputs)? {
            verify_outputs(&prepared)?;
            return Ok(reference);
        }
    }
    let temporary = TempDir::new(out)?;
    preparation::prepare_package(&package.directory, profile, os, &temporary.0)?;
    if identity.provider == "simulation" {
        crate::write_simulation_config(profile, &temporary.0)?;
    }
    let mut directives = if ["linux", "macos", "nuttx"].contains(&identity.provider.as_str()) {
        native::build_with_context(&package.directory, os, target, &temporary.0, &context)?
    } else {
        Vec::new()
    };
    let paths = identity
        .inputs
        .iter()
        .map(|input| input.path.clone())
        .chain(directives.iter().filter_map(|line| {
            line.strip_prefix("cargo:rerun-if-changed=")
                .map(PathBuf::from)
        }));
    let inputs = snapshot(paths)?;
    if !unchanged(&identity.inputs)? {
        return Err(
            "preparation inputs changed while generating or compiling; prepare again".into(),
        );
    }
    let key = hash_bytes(&bytes(&(&identity, &inputs))?);
    let destination = out.join("packages").join(key);
    fs::create_dir_all(destination.parent().unwrap()).map_err(|error| error.to_string())?;
    let source_search = format!(
        "cargo:rustc-link-search=native={}",
        path_text(&temporary.0)?
    );
    for line in &mut directives {
        if *line == source_search {
            *line = format!(
                "cargo:rustc-link-search=native={}",
                path_text(&destination)?
            );
        }
        if line.contains(['\r', '\n']) {
            return Err("prepared Cargo directive contains a line break".into());
        }
    }
    let mut outputs = Vec::new();
    for entry in fs::read_dir(&temporary.0).map_err(|error| error.to_string())? {
        let entry = entry.map_err(|error| error.to_string())?;
        if !entry
            .file_type()
            .map_err(|error| error.to_string())?
            .is_file()
        {
            return Err("unexpected directory in prepared package output".into());
        }
        outputs.push(File {
            path: destination.join(entry.file_name()),
            sha256: hash(&entry.path())?,
        });
    }
    outputs.sort_by(|a, b| a.path.cmp(&b.path));
    let prepared = Prepared {
        identity,
        inputs,
        outputs,
        directives,
    };
    fs::write(temporary.0.join("prepared.json"), bytes(&prepared)?)
        .map_err(|error| error.to_string())?;
    match fs::rename(&temporary.0, &destination) {
        Ok(()) => {}
        Err(_) if destination.is_dir() => {
            // Another controller may have published this same input identity.
            // Use its complete record, never a partially populated directory.
            let existing: Prepared = read(&destination.join("prepared.json"))?;
            if existing.identity != prepared.identity || existing.inputs != prepared.inputs {
                return Err("concurrent preparation published different inputs".into());
            }
            verify_outputs(&existing)?;
        }
        Err(error) => return Err(format!("{}: {error}", destination.display())),
    }
    let reference = Reference {
        package: prepared.identity.package,
        directory: prepared.identity.directory,
        record: file(&destination.join("prepared.json"))?,
    };
    write(&index, &bytes(&reference)?)?;
    Ok(reference)
}

/// Prepare only the packages selected by Cargo. All generated files and native
/// archives are outside source and published atomically under their input hash.
/// The caller supplies an immutable resolved-profile receipt and passes the
/// returned path to Cargo through `NXRS_PREPARED_BUILD`.
#[allow(clippy::too_many_arguments)]
pub fn prepare(
    root: &Path,
    profile: &Profile,
    target: &str,
    os: &str,
    profile_receipt: &Path,
    packages: &[Package],
    command: &Command,
    out: &Path,
) -> Result<PathBuf> {
    let profile = crate::resolved::load(profile_receipt, root, Some(profile.name()), target, os)?;
    let out = absolute(out)?;
    fs::create_dir_all(&out).map_err(|error| error.to_string())?;
    let mut prepared = Vec::new();
    let mut seen = BTreeSet::new();
    for package in packages {
        let reference = prepare_package(
            package,
            &profile,
            profile_receipt,
            target,
            os,
            command,
            &out,
        )?;
        if !seen.insert(reference.directory.clone()) {
            return Err("duplicate consumed package in preparation".into());
        }
        prepared.push(reference);
    }
    prepared.sort_by(|a, b| a.directory.cmp(&b.directory));
    let receipt = Receipt {
        version: VERSION,
        generator: generator(),
        profile: file(profile_receipt)?,
        target: target.into(),
        os: os.into(),
        packages: prepared,
    };
    let data = bytes(&receipt)?;
    let path = out.join(format!("receipt-{}.json", hash_bytes(&data)));
    write(&path, &data)?;
    Ok(path)
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Delivered {
    version: u32,
    files: BTreeSet<String>,
}

fn rust_output(name: &str) -> bool {
    name.len() > 3
        && name.ends_with(".rs")
        && !name.starts_with('.')
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

fn delivered(out: &Path) -> Result<BTreeSet<String>> {
    let path = out.join("nxrs-prepared-files.json");
    if !path.exists() {
        return Ok(BTreeSet::new());
    }
    let previous: Delivered = read(&path)?;
    if previous.version != VERSION || !previous.files.iter().all(|name| rust_output(name)) {
        return Err("invalid prepared output ownership record".into());
    }
    Ok(previous.files)
}

/// Clean only files owned by this transport or the preceding generic renderer.
/// A no-profile invocation must not see a previous product's generated constants.
pub fn clear(out: &Path, package: &str) -> Result<()> {
    for name in delivered(out)? {
        crate::remove_file(&out.join(name))?;
    }
    crate::remove_file(&out.join("nxrs-prepared-files.json"))?;
    preparation::clear(out, package)
}

fn preserve_stamp(path: &Path, source: &Path) -> Result<()> {
    let modified = fs::metadata(source)
        .and_then(|metadata| metadata.modified())
        .map_err(|error| error.to_string())?;
    if fs::metadata(path)
        .and_then(|metadata| metadata.modified())
        .ok()
        != Some(modified)
    {
        fs::OpenOptions::new()
            .write(true)
            .open(path)
            .and_then(|file| file.set_modified(modified))
            .map_err(|error| format!("{}: {error}", path.display()))?;
    }
    Ok(())
}

fn deliver(prepared: &Prepared, record: &Path, out: &Path) -> Result<()> {
    let previous = delivered(out)?;
    preparation::clear(out, &prepared.identity.package)?;
    let mut files = BTreeSet::new();
    for source in &prepared.outputs {
        let name = source
            .path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or("invalid prepared output name")?;
        if !rust_output(name) {
            continue;
        }
        write(
            &out.join(name),
            &fs::read(&source.path).map_err(|error| error.to_string())?,
        )?;
        // Cargo compares watched files with the build-script invocation time.
        // These inputs were generated before Cargo; retain that timestamp when
        // delivering them so the very next command is already a no-op.
        preserve_stamp(&out.join(name), &source.path)?;
        files.insert(name.to_owned());
    }
    for name in previous.difference(&files) {
        crate::remove_file(&out.join(name))?;
    }
    let path = out.join("nxrs-prepared-files.json");
    write(
        &path,
        &bytes(&Delivered {
            version: VERSION,
            files,
        })?,
    )?;
    preserve_stamp(&path, record)
}

/// Cargo-side transport. This validates current inputs and copies already
/// generated Rust files; it never renders configuration or compiles native code.
pub fn apply(
    path: &Path,
    directory: &Path,
    package: &str,
    profile: &Profile,
    target: &str,
    os: &str,
    out: &Path,
) -> Result<Vec<String>> {
    if !path.is_absolute() {
        return Err("NXRS_PREPARED_BUILD must be an absolute path".into());
    }
    path_text(path)?;
    let receipt: Receipt = read(path)?;
    if receipt.version != VERSION || receipt.generator != generator() {
        return Err(
            "prepared build uses another generator; prepare again with the consumed build support"
                .into(),
        );
    }
    if receipt.target != target || receipt.os != os {
        return Err("prepared build target does not match this Cargo build; prepare again".into());
    }
    let profile_path = std::env::var_os("NXRS_RESOLVED_PROFILE")
        .map(PathBuf::from)
        .ok_or("NXRS_PREPARED_BUILD requires its explicit NXRS_RESOLVED_PROFILE")?;
    if file(&profile_path)? != receipt.profile {
        return Err("prepared build does not match the resolved profile; prepare again".into());
    }
    let directory = directory
        .canonicalize()
        .map_err(|error| error.to_string())?;
    let mut directives = vec![
        format!("cargo:rerun-if-changed={}", path_text(path)?),
        format!(
            "cargo:rerun-if-changed={}",
            path_text(&receipt.profile.path)?
        ),
    ];
    let Some(reference) = receipt
        .packages
        .iter()
        .find(|entry| entry.directory == directory && entry.package == package)
    else {
        let capability = package.strip_prefix("nxrs-").ok_or("HAL package name")?;
        if profile.binding(capability) == "none" {
            clear(out, package)?;
            return Ok(directives);
        }
        return Err(format!("{package} was not prepared in the target production graph; prepare again for this Cargo selection"));
    };
    let prepared = load(reference)?;
    let context = native::Context::from_build_script()?;
    if context.feature_environment() != &prepared.identity.features {
        return Err(format!("prepared features for {package} do not match this Cargo unit; prepare again for this feature selection"));
    }
    let native_variables = prepared
        .identity
        .native
        .environment
        .keys()
        .map(String::as_str)
        .collect::<Vec<_>>();
    if context.environment_inputs(&native_variables)? != prepared.identity.native.environment {
        return Err(format!("prepared native environment for {package} differs from Cargo; supply native tool/include settings in the invoking controller environment and prepare again (Cargo-only [env] overrides are not supported yet)"));
    }
    let current = identity(
        &Package {
            identity: prepared.identity.cargo_identity.clone(),
            directory,
            // Cargo's environment projection was checked above. The controller
            // rejects declared feature names whose projections collide.
            features: prepared.identity.cargo_features.clone(),
        },
        profile,
        &profile_path,
        target,
        os,
        &context,
    )?;
    if current != prepared.identity || !unchanged(&prepared.inputs)? {
        return Err(format!(
            "stale prepared inputs for {package}; run the build controller to prepare again"
        ));
    }
    verify_outputs(&prepared)?;
    directives.extend(prepared.directives.iter().cloned());
    directives.push(format!(
        "cargo:rerun-if-changed={}",
        path_text(&reference.record.path)?
    ));
    for input in &prepared.inputs {
        directives.push(format!(
            "cargo:rerun-if-changed={}",
            path_text(&input.path)?
        ));
    }
    for output in &prepared.outputs {
        directives.push(format!(
            "cargo:rerun-if-changed={}",
            path_text(&output.path)?
        ));
    }
    for name in prepared.identity.native.environment.keys() {
        directives.push(format!("cargo:rerun-if-env-changed={name}"));
    }
    deliver(&prepared, &reference.record.path, out)?;
    // Cargo also tracks the transported copies. Editing/removing one must run
    // this adapter again before rustc or rustdoc can consume that copy.
    for name in delivered(out)? {
        directives.push(format!(
            "cargo:rerun-if-changed={}",
            path_text(&out.join(name))?
        ));
    }
    directives.push(format!(
        "cargo:rerun-if-changed={}",
        path_text(&out.join("nxrs-prepared-files.json"))?
    ));
    Ok(directives)
}
