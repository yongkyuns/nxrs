//! Ask Cargo which production dependencies and features this build actually selects.
//!
//! `cargo tree` uses the selected package, features, target, compiler, and Cargo
//! configuration. Metadata supplies declared dependency aliases and package paths;
//! it is never the authority for production feature sets. This module does not
//! traverse dependencies or evaluate Cargo's cfg/feature language.
use crate::{Result, profile::Profile};
use serde_json::{Value, json};
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
    /// Additive implementation features verified and appended through declared edges.
    pub implementation_features: Vec<String>,
}
impl Selection {
    pub fn json(&self) -> Value {
        json!({
            "status":"resolved", "packages":self.packages,
            "model_features":self.model_features,
            "implementation_features":self.implementation_features,
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
/// only profile features that Cargo verified for that invocation.
///
/// Call this after setting the actual compiler, target (including custom JSON),
/// configuration, user features, and default-feature selection. The build command
/// is unchanged on failure. SDK-free planning must leave this resolution pending.
pub fn resolve(build: &mut Command, profile: &Profile) -> Result<Selection> {
    let query = Query::from_build(build)?;
    let baseline = query.graph(build, &[])?;
    let mut requests = Vec::new();
    if let Some(detector) = profile.detector()? {
        let (wanted, conflicting) = if detector.model == "pico" {
            ("pico-model", "person-model")
        } else {
            ("person-model", "pico-model")
        };
        if let Some(request) = Request::consumed(
            &baseline,
            "nxrs-detector",
            BTreeSet::from([wanted.to_owned()]),
            Some(conflicting),
        )? {
            requests.push(request);
        }
    }
    for (package, features) in profile.implementation_features()? {
        if let Some(request) = Request::consumed(&baseline, &package, features, None)? {
            requests.push(request);
        }
    }
    for request in &requests {
        request.validate_conflict(&baseline, profile)?;
    }
    for request in requests
        .iter()
        .filter(|request| request.conflicting.is_none())
    {
        request.validate_declarations(&query, build, profile)?;
    }

    let mut resolved = baseline;
    let mut additions = Vec::new();
    let mut model_features = Vec::new();
    let mut implementation_features = Vec::new();
    let mut metadata = None;
    for request in &requests {
        for wanted in &request.features {
            if request.has_feature(&resolved, wanted) {
                continue;
            }
            if metadata.is_none() {
                metadata = Some(query.metadata(build)?);
            }
            let app = package_at_manifest(metadata.as_ref().unwrap(), &query.manifest)?;
            let candidates = request.candidates(app, wanted)?;
            let mut selected = None;
            let mut failure = None;
            for feature in candidates {
                // Query the whole accumulated request, not independent graphs
                // whose feature/default interactions have never been resolved.
                let mut trial = additions.clone();
                trial.push(feature.clone());
                match query.graph(build, &trial) {
                    Ok(graph) if request.has_feature(&graph, wanted) => {
                        if let Err(error) = requests.iter().try_for_each(|request| {
                            request.validate_identity(&graph)?;
                            request.validate_conflict(&graph, profile)
                        }) {
                            failure = Some(error);
                            continue;
                        }
                        selected = Some((feature, graph));
                        break;
                    }
                    Ok(_) => {}
                    Err(error) => failure = Some(error),
                }
            }
            let Some((feature, graph)) = selected else {
                let mut error = request.unreachable(profile, wanted);
                if let Some(failure) = failure {
                    error.push_str(&format!("; {failure}"));
                }
                return Err(error);
            };
            additions.push(feature.clone());
            if request.conflicting.is_some() {
                model_features.push(feature);
            } else {
                implementation_features.push(feature);
            }
            resolved = graph;
        }
    }
    for request in &requests {
        request.validate_identity(&resolved)?;
        request.validate_conflict(&resolved, profile)?;
        if !request
            .features
            .iter()
            .all(|feature| request.has_feature(&resolved, feature))
        {
            return Err(format!(
                "Cargo's combined feature selection did not preserve the required features for {}",
                request.identity
            ));
        }
    }
    // Do not partially configure the real command if any package, feature, or
    // detector conflict failed. All additions share the final verified graph.
    for feature in additions {
        build.args(["--features", &feature]);
    }
    Ok(Selection {
        packages: resolved.into_keys().collect(),
        model_features,
        implementation_features,
    })
}

struct Request {
    name: String,
    identity: String,
    depth: usize,
    features: BTreeSet<String>,
    // Detector policy is deliberately separate from additive device features.
    conflicting: Option<&'static str>,
}
impl Request {
    fn consumed(
        graph: &Graph,
        name: &str,
        features: BTreeSet<String>,
        conflicting: Option<&'static str>,
    ) -> Result<Option<Self>> {
        let mut packages = graph.iter().filter(|(_, package)| package.name == name);
        let Some((identity, package)) = packages.next() else {
            // Board records must not pull unused HALs or optional services in.
            return Ok(None);
        };
        if packages.next().is_some() {
            return Err(format!(
                "the selected app graph contains multiple {name} packages"
            ));
        }
        Ok(Some(Self {
            name: name.into(),
            identity: identity.clone(),
            depth: package.depth,
            features,
            conflicting,
        }))
    }

    fn has_feature(&self, graph: &Graph, feature: &str) -> bool {
        graph
            .get(&self.identity)
            .is_some_and(|package| package.features.contains(feature))
    }

    fn validate_identity(&self, graph: &Graph) -> Result<()> {
        if graph
            .values()
            .filter(|package| package.name == self.name)
            .count()
            > 1
        {
            return Err(format!(
                "the combined app graph contains multiple {} packages",
                self.name
            ));
        }
        if !graph.contains_key(&self.identity) {
            return Err(format!(
                "the combined app graph no longer contains the consumed package {}",
                self.identity
            ));
        }
        Ok(())
    }

    fn validate_declarations(
        &self,
        query: &Query,
        build: &Command,
        profile: &Profile,
    ) -> Result<()> {
        // Full metadata fetches unselected dev/workspace/target dependencies,
        // even with --filter-platform. Path-backed SDKs need only package-local
        // --no-deps metadata. Treat the displayed path as a candidate, then ask
        // Cargo to verify its structured ID against the original production
        // identity. Do not infer Git/registry cache paths or substitute the SDK.
        let directory = self.identity.split_once(" (")
            .and_then(|(_, source)| source.strip_suffix(')'))
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .ok_or_else(|| format!(
                "declarative implementation features currently require a path-backed capability package; Cargo selected {}; use an SDK checkout as a path dependency",
                self.identity
            ))?;
        let manifest = directory
            .join("Cargo.toml")
            .canonicalize()
            .map_err(|error| format!("{}: {error}", directory.display()))?;
        let metadata = query.package_metadata(build, &manifest)?;
        let package = package_at_manifest(&metadata, &manifest)?;
        let id = package["id"]
            .as_str()
            .ok_or("Cargo package has no identity")?;
        if !query
            .package_identity(build, id)?
            .contains_key(&self.identity)
        {
            return Err(format!(
                "Cargo package identity changed for {}",
                self.identity
            ));
        }
        let actual = nxrs_build_support::bindings::Package::load(&directory)?;
        for (_, expected) in profile
            .catalogs()
            .packages()
            .filter(|(_, package)| package.name == self.name)
        {
            if !actual.same_declarations(expected) {
                return Err(format!(
                    "binding declarations for {} differ from the resolved profile; use matching package catalogs and schemas",
                    self.identity
                ));
            }
        }
        Ok(())
    }

    fn validate_conflict(&self, graph: &Graph, profile: &Profile) -> Result<()> {
        if let Some(conflicting) = self.conflicting {
            if self.has_feature(graph, conflicting) {
                return Err(format!(
                    "profile {} requires {}, but Cargo selected {conflicting} for {}",
                    profile.name(),
                    self.features.iter().next().unwrap(),
                    self.identity
                ));
            }
        }
        Ok(())
    }

    fn candidates(&self, app: &Value, feature: &str) -> Result<BTreeSet<String>> {
        let mut candidates = BTreeSet::new();
        if self.depth == 0 {
            candidates.insert(feature.to_owned());
        } else if self.conflicting.is_none() || self.depth == 1 {
            for dependency in app["dependencies"]
                .as_array()
                .ok_or("Cargo metadata did not contain application dependencies")?
            {
                if dependency["name"] != self.name || !dependency["kind"].is_null() {
                    continue;
                }
                let alias = dependency["rename"]
                    .as_str()
                    .or_else(|| dependency["name"].as_str())
                    .ok_or("Cargo dependency did not have a name")?;
                // Preserve the detector's existing enabled-edge policy. A device
                // request may activate a dormant direct composition edge only
                // because this exact HAL is already consumed in the baseline.
                // A weak request would not reach it through that dormant edge.
                let weak = if self.conflicting.is_some()
                    && dependency["optional"].as_bool() == Some(true)
                {
                    "?"
                } else {
                    ""
                };
                candidates.insert(format!("{alias}{weak}/{feature}"));
            }
        }
        Ok(candidates)
    }

    fn unreachable(&self, profile: &Profile, feature: &str) -> String {
        if self.conflicting.is_some() {
            format!(
                "profile {} selects {feature} for {}, but Cargo cannot enable that model through this app's dependencies; declare nxrs-detector as an enabled direct application dependency (renaming and workspace inheritance are supported), without selecting a model in the manifest",
                profile.name(),
                self.identity
            )
        } else {
            format!(
                "profile {} requires implementation feature {feature} for {}, but Cargo cannot enable it through this app's dependencies; declare {} as a direct application dependency (an optional alias and workspace inheritance are supported), without selecting hardware features in the manifest; the feature request must reach the already consumed package on this target",
                profile.name(),
                self.identity,
                self.name
            )
        }
    }
}

fn package_at_manifest<'a>(metadata: &'a Value, manifest: &PathBuf) -> Result<&'a Value> {
    metadata["packages"]
        .as_array()
        .ok_or("Cargo metadata did not return packages")?
        .iter()
        .find(|package| {
            package["manifest_path"]
                .as_str()
                .and_then(|path| PathBuf::from(path).canonicalize().ok())
                .is_some_and(|path| path == *manifest)
        })
        .ok_or_else(|| format!("Cargo metadata did not contain {}", manifest.display()))
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
            if destination > 1 && flag != "--manifest-path" {
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

    fn graph(&self, build: &Command, features: &[String]) -> Result<Graph> {
        parse_tree(&capture(&mut self.tree_command(build, features))?)
    }

    fn tree_command(&self, build: &Command, features: &[String]) -> Command {
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
        for feature in features {
            query.args(["--features", feature]);
        }
        query
    }

    fn package_identity(&self, build: &Command, id: &str) -> Result<Graph> {
        let mut query = self.tree_command(build, &[]);
        query.args(["--invert", id, "--depth", "0"]);
        parse_tree(&capture(&mut query)?)
    }

    fn package_metadata(&self, build: &Command, manifest: &PathBuf) -> Result<Value> {
        let mut query = self.command(build, "metadata");
        query
            .args(&self.metadata)
            .args([
                "--no-deps",
                "--format-version",
                "1",
                "--color",
                "never",
                "--manifest-path",
            ])
            .arg(manifest);
        serde_json::from_str(&capture(&mut query)?)
            .map_err(|error| format!("invalid Cargo metadata: {error}"))
    }

    fn metadata(&self, build: &Command) -> Result<Value> {
        self.package_metadata(build, &self.manifest)
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
