//! Ask Cargo which production dependencies and features this build actually selects.
//!
//! `cargo tree` uses the selected package, features, target, compiler, and Cargo
//! configuration. Metadata is used only for the app's declared dependency aliases;
//! this module does not traverse manifests or evaluate Cargo's cfg/feature language.
use crate::{profile::Profile, Result};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::{OsStr, OsString},
    path::PathBuf,
    process::Command,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Selection {
    /// Cargo's package descriptions include version and source, not just a name.
    pub packages: Vec<String>,
    /// Profile features that were verified by Cargo and appended to the build.
    pub model_features: Vec<String>,
}
impl Selection {
    pub fn json(&self) -> Value {
        json!({"status":"resolved","packages":self.packages,"model_features":self.model_features})
    }
}

#[derive(Debug)]
struct Package {
    name: String,
    features: BTreeSet<String>,
    depth: usize,
}
type Graph = BTreeMap<String, Package>;

/// Resolve a fully configured production `cargo build` invocation, then append
/// only the profile model feature that Cargo verified for that invocation.
///
/// Call this after setting the actual compiler, target (including custom JSON),
/// configuration, user features, and default-feature selection. The build command
/// is unchanged on failure. SDK-free planning must leave this resolution pending.
pub fn resolve(build: &mut Command, profile: &Profile) -> Result<Selection> {
    let query = Query::from_build(build)?;
    let graph = query.graph(build, None)?;
    let Some(detector) = profile.detector()? else {
        return Ok(selection(graph, vec![]));
    };
    let selected: Vec<_> = graph
        .iter()
        .filter(|(_, package)| package.name == "nxrs-detector")
        .collect();
    if selected.is_empty() {
        return Ok(selection(graph, vec![]));
    }
    if selected.len() != 1 {
        return Err("the selected app graph contains multiple nxrs-detector packages".into());
    }
    let (identity, package) = selected[0];
    let wanted = if detector.model == "pico" {
        "pico-model"
    } else {
        "person-model"
    };
    let conflicting = if wanted == "pico-model" {
        "person-model"
    } else {
        "pico-model"
    };
    if package.features.contains(conflicting) {
        return Err(format!(
            "profile {} requires {wanted}, but Cargo selected {conflicting} for {identity}",
            profile.name()
        ));
    }
    if package.features.contains(wanted) {
        return Ok(selection(graph, vec![]));
    }

    // Cargo can select features of direct dependencies, including renamed and
    // workspace-inherited dependencies. It cannot address an arbitrary transitive
    // dependency's feature from an external consumer. Keep that composition edge
    // explicit instead of synthesizing manifests or service feature forwarding.
    let metadata = query.metadata(build)?;
    let app = metadata["packages"]
        .as_array()
        .ok_or("Cargo metadata did not return packages")?
        .iter()
        .find(|package| {
            package["manifest_path"]
                .as_str()
                .and_then(|path| PathBuf::from(path).canonicalize().ok())
                .is_some_and(|path| path == query.manifest)
        })
        .ok_or("Cargo metadata did not contain the application manifest")?;
    let mut candidates = BTreeSet::new();
    if package.depth == 0 {
        candidates.insert(wanted.to_owned());
    } else if package.depth == 1 {
        for dependency in app["dependencies"]
            .as_array()
            .ok_or("Cargo metadata did not contain application dependencies")?
        {
            if dependency["name"] != "nxrs-detector" || !dependency["kind"].is_null() {
                continue;
            }
            let alias = dependency["rename"]
                .as_str()
                .or_else(|| dependency["name"].as_str())
                .ok_or("Cargo dependency did not have a name")?;
            // A weak feature edge cannot turn on a disabled optional dependency.
            let weak = if dependency["optional"].as_bool() == Some(true) {
                "?"
            } else {
                ""
            };
            candidates.insert(format!("{alias}{weak}/{wanted}"));
        }
    }
    for feature in candidates {
        let resolved = query.graph(build, Some(&feature))?;
        if resolved.get(identity).is_some_and(|package| {
            package.features.contains(wanted) && !package.features.contains(conflicting)
        }) {
            build.args(["--features", &feature]);
            return Ok(selection(resolved, vec![feature]));
        }
    }
    Err(format!(
        "profile {} selects {wanted} for {identity}, but Cargo cannot enable that model through this app's dependencies; declare nxrs-detector as an enabled direct application dependency (renaming and workspace inheritance are supported), without selecting a model in the manifest",
        profile.name()
    ))
}

fn selection(graph: Graph, model_features: Vec<String>) -> Selection {
    Selection {
        packages: graph.into_keys().collect(),
        model_features,
    }
}

/// Projection of our build invocation onto Cargo's graph queries. Unknown build
/// flags fail closed so a new graph-affecting frontend option cannot be dropped.
struct Query {
    prefix: Vec<OsString>,
    tree: Vec<OsString>,
    metadata: Vec<OsString>,
    manifest: PathBuf,
}
impl Query {
    fn from_build(build: &Command) -> Result<Self> {
        let mut args = build.get_args().peekable();
        let mut prefix = vec![];
        while args
            .peek()
            .is_some_and(|arg| arg.to_string_lossy().starts_with('+'))
        {
            prefix.push(args.next().unwrap().to_owned());
        }
        if args.next() != Some(OsStr::new("build")) {
            return Err("dependency resolution requires a configured cargo build command".into());
        }
        let mut tree = vec![];
        let mut metadata = vec![];
        let mut manifest = None;
        while let Some(arg) = args.next() {
            let option = arg.to_str().ok_or("non-UTF-8 Cargo option")?;
            let (flag, inline) = option
                .split_once('=')
                .map_or((option, None), |(flag, value)| (flag, Some(value)));
            let destination = match flag {
                "--manifest-path" | "--lockfile-path" | "--config" | "-Z" => 2,
                "--features" | "-F" | "--target" | "--package" | "-p" | "--exclude" => 1,
                "--bin" | "--profile" | "--target-dir" | "--message-format" | "--jobs" | "-j"
                | "--color" | "--artifact-dir" | "--out-dir" => 0,
                "--locked" | "--offline" | "--frozen" => {
                    tree.push(arg.to_owned());
                    metadata.push(arg.to_owned());
                    continue;
                }
                "--no-default-features" | "--all-features" | "--workspace" => {
                    tree.push(arg.to_owned());
                    continue;
                }
                "--release"
                | "--lib"
                | "--bins"
                | "--keep-going"
                | "--future-incompat-report"
                | "--verbose"
                | "-v"
                | "-vv"
                | "--quiet"
                | "-q"
                | "--timings" => continue,
                _ if option.starts_with("-Z") => {
                    tree.push(arg.to_owned());
                    metadata.push(arg.to_owned());
                    continue;
                }
                _ => {
                    return Err(format!(
                        "unsupported Cargo build option for dependency resolution: {option}"
                    ));
                }
            };
            let value = inline
                .map(OsStr::new)
                .or_else(|| args.next())
                .ok_or_else(|| format!("Cargo {flag} is missing a value"))?;
            if flag == "--manifest-path" {
                let path = PathBuf::from(value);
                manifest = Some(if path.is_absolute() {
                    path
                } else {
                    build
                        .get_current_dir()
                        .unwrap_or(OsStr::new(".").as_ref())
                        .join(path)
                });
            }
            if destination > 0 {
                tree.extend([OsString::from(flag), value.to_owned()]);
            }
            if destination > 1 {
                metadata.extend([OsString::from(flag), value.to_owned()]);
            }
        }
        Ok(Self {
            prefix,
            tree,
            metadata,
            manifest: manifest
                .ok_or("Cargo dependency resolution requires --manifest-path")?
                .canonicalize()
                .map_err(|error| error.to_string())?,
        })
    }

    fn command(&self, build: &Command, action: &str) -> Command {
        let mut query = Command::new(build.get_program());
        query.args(&self.prefix).arg(action);
        if let Some(directory) = build.get_current_dir() {
            query.current_dir(directory);
        }
        for (name, value) in build.get_envs() {
            if let Some(value) = value {
                query.env(name, value);
            } else {
                query.env_remove(name);
            }
        }
        query
    }

    fn graph(&self, build: &Command, feature: Option<&str>) -> Result<Graph> {
        let mut query = self.command(build, "tree");
        query.args(&self.tree).args([
            "--edges",
            "normal,no-proc-macro",
            "--prefix",
            "depth",
            "--format",
            "\t{p}\t{f}",
            "--charset",
            "ascii",
            "--color",
            "never",
        ]);
        if let Some(feature) = feature {
            query.args(["--features", feature]);
        }
        parse_tree(&capture(&mut query)?)
    }

    fn metadata(&self, build: &Command) -> Result<Value> {
        let mut query = self.command(build, "metadata");
        query
            .args(&self.metadata)
            .args(["--no-deps", "--format-version", "1", "--color", "never"]);
        serde_json::from_str(&capture(&mut query)?)
            .map_err(|error| format!("invalid Cargo metadata: {error}"))
    }
}

fn capture(command: &mut Command) -> Result<String> {
    let output = command
        .output()
        .map_err(|error| format!("Cargo dependency resolution failed: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "Cargo dependency resolution failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    String::from_utf8(output.stdout).map_err(|error| format!("invalid Cargo graph output: {error}"))
}

fn parse_tree(text: &str) -> Result<Graph> {
    let mut graph = Graph::new();
    for line in text.lines().filter(|line| !line.is_empty()) {
        let line = line.strip_suffix(" (*)").unwrap_or(line);
        let mut fields = line.splitn(3, '\t');
        let depth: usize = fields
            .next()
            .unwrap_or("")
            .parse()
            .map_err(|_| "invalid Cargo tree depth")?;
        let identity = fields.next().ok_or("missing Cargo package description")?;
        let features = fields.next().ok_or("missing Cargo package features")?;
        let (name, _) = identity
            .split_once(" v")
            .ok_or("invalid Cargo package description")?;
        let package = graph.entry(identity.to_owned()).or_insert_with(|| Package {
            name: name.into(),
            features: BTreeSet::new(),
            depth,
        });
        package.depth = package.depth.min(depth);
        package.features.extend(
            features
                .split(',')
                .filter(|feature| !feature.is_empty())
                .map(str::to_owned),
        );
    }
    if graph.is_empty() {
        return Err("Cargo did not select an application dependency graph".into());
    }
    Ok(graph)
}
