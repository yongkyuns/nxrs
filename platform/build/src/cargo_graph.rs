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
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Selection {
    /// Cargo's package descriptions include version and source, not just a name.
    pub packages: Vec<String>,
    features: BTreeMap<String, BTreeSet<String>>,
    /// Profile features that were verified by Cargo and appended to the build.
    pub model_features: Vec<String>,
    pub driver_features: Vec<String>,
}
impl Selection {
    pub fn json(&self) -> Value {
        json!({"status":"resolved","packages":self.packages,"model_features":self.model_features,"driver_features":self.driver_features})
    }

    pub fn path_packages(&self) -> impl Iterator<Item = (&str, &Path, &BTreeSet<String>)> {
        self.features.iter().filter_map(|(identity, features)| {
            let (_, source) = identity.split_once(" (")?;
            let directory = Path::new(source.strip_suffix(')')?);
            directory
                .is_absolute()
                .then_some((identity.as_str(), directory, features))
        })
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
/// the additive driver and model features Cargo verified for that invocation.
///
/// Call this after setting the actual compiler, target (including custom JSON),
/// configuration, user features, and default-feature selection. The build command
/// is unchanged on failure. SDK-free planning must leave this resolution pending.
pub fn resolve(build: &mut Command, profile: &Profile) -> Result<Selection> {
    // Prepare a complete proposal before changing the caller's command.
    let mut trial = Command::new(build.get_program());
    trial.args(build.get_args());
    if let Some(cwd) = build.get_current_dir() {
        trial.current_dir(cwd);
    }
    for (key, value) in build.get_envs() {
        if let Some(value) = value {
            trial.env(key, value);
        } else {
            trial.env_remove(key);
        }
    }
    let composed = profile.composition()?.is_some();
    // A successful feature trial is the graph for the updated command.
    let mut graph = Query::from_build(&trial)?.graph(&trial, None)?;
    let mut driver_features = Vec::new();
    if composed {
        for (capability, catalogue) in profile.catalogs().packages() {
            if profile.binding(capability) != "nuttx" {
                continue;
            }
            let required = profile.driver_features(capability)?;
            for wanted in required {
                let packages: Vec<_> = graph
                    .iter()
                    .filter(|(_, p)| p.name == catalogue.name)
                    .collect();
                if packages.is_empty() {
                    continue;
                }
                if packages.len() != 1 {
                    return Err(format!(
                        "multiple {} packages in the product graph",
                        catalogue.name
                    ));
                }
                let (identity, package) = packages[0];
                if package.features.contains(&wanted) {
                    continue;
                }
                let query = Query::from_build(&trial)?;
                let candidates = query.feature_candidates(&trial, package, &wanted)?;
                let mut selected = None;
                for candidate in candidates {
                    let resolved = query.graph(&trial, Some(&candidate))?;
                    if resolved
                        .get(identity)
                        .is_some_and(|p| p.features.contains(&wanted))
                    {
                        selected = Some((candidate, resolved));
                        break;
                    }
                }
                let (feature, resolved) = selected.ok_or_else(|| {
                    format!(
                        "{} requires {wanted}; declare {} as a direct catalogue dependency",
                        profile.name(),
                        catalogue.name
                    )
                })?;
                trial.args(["--features", &feature]);
                graph = resolved;
                driver_features.push(feature);
            }
        }
    }
    let model_features = resolve_model(&mut trial, profile, &mut graph)?;
    for (capability, catalogue) in profile.catalogs().packages() {
        let required = profile.driver_features(capability)?;
        if required.is_empty() {
            continue;
        }
        let consumed: Vec<_> = graph
            .values()
            .filter(|p| p.name == catalogue.name)
            .collect();
        if consumed.len() > 1
            || consumed
                .first()
                .is_some_and(|p| !required.is_subset(&p.features))
        {
            return Err(format!(
                "combined feature selection does not satisfy {}",
                catalogue.name
            ));
        }
    }
    build.args(trial.get_args().skip(build.get_args().count()));
    Ok(Selection {
        packages: graph.keys().cloned().collect(),
        features: graph
            .into_iter()
            .map(|(identity, package)| (identity, package.features))
            .collect(),
        model_features,
        driver_features,
    })
}

fn resolve_model(build: &mut Command, profile: &Profile, graph: &mut Graph) -> Result<Vec<String>> {
    let Some(detector) = profile.detector()? else {
        return Ok(vec![]);
    };
    let selected: Vec<_> = graph
        .iter()
        .filter(|(_, package)| package.name == "nxrs-detector")
        .collect();
    if selected.is_empty() {
        return Ok(vec![]);
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
        return Ok(vec![]);
    }

    // Cargo can select features of direct dependencies, including renamed and
    // workspace-inherited dependencies. It cannot address an arbitrary transitive
    // dependency's feature from an external consumer. Keep that composition edge
    // explicit instead of synthesizing manifests or service feature forwarding.
    let query = Query::from_build(build)?;
    let candidates = query.feature_candidates(build, package, wanted)?;
    for feature in candidates {
        let resolved = query.graph(build, Some(&feature))?;
        if resolved.get(identity).is_some_and(|package| {
            package.features.contains(wanted) && !package.features.contains(conflicting)
        }) {
            build.args(["--features", &feature]);
            *graph = resolved;
            return Ok(vec![feature]);
        }
    }
    Err(format!(
        "profile {} selects {wanted} for {identity}, but Cargo cannot enable that model through this app's dependencies; declare nxrs-detector as an enabled direct application dependency (renaming and workspace inheritance are supported), without selecting a model in the manifest",
        profile.name()
    ))
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

    fn feature_candidates(
        &self,
        build: &Command,
        package: &Package,
        wanted: &str,
    ) -> Result<BTreeSet<String>> {
        if package.depth == 0 {
            return Ok(BTreeSet::from([wanted.to_owned()]));
        }
        if package.depth != 1 {
            return Ok(BTreeSet::new());
        }
        let metadata = self.metadata(build)?;
        let app = metadata["packages"]
            .as_array()
            .ok_or("missing Cargo packages")?
            .iter()
            .find(|p| {
                p["manifest_path"]
                    .as_str()
                    .and_then(|p| PathBuf::from(p).canonicalize().ok())
                    .is_some_and(|p| p == self.manifest)
            })
            .ok_or("missing application manifest")?;
        app["dependencies"]
            .as_array()
            .ok_or("missing Cargo dependencies")?
            .iter()
            .filter(|d| d["name"] == package.name && d["kind"].is_null())
            .map(|d| {
                let alias = d["rename"]
                    .as_str()
                    .or(d["name"].as_str())
                    .ok_or("missing dependency name")?;
                // A weak edge cannot activate an unconsumed optional catalogue.
                Ok(format!(
                    "{alias}{}/{wanted}",
                    if d["optional"] == true { "?" } else { "" }
                ))
            })
            .collect()
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
