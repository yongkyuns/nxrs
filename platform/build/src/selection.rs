//! Cargo, not a second manifest walker, owns enabled dependency selection.
use crate::{build::Options, output, Result};
use serde_json::Value;
use std::{collections::{BTreeMap, BTreeSet}, path::Path, process::Command};

#[derive(Debug)]
pub struct CargoSelection {
    pub packages: BTreeSet<String>,
}
impl CargoSelection {
    pub fn resolve(manifest: &Path, options: &Options, target: &Path, mut cargo: Command) -> Result<Self> {
        cargo.args(["metadata", "--format-version", "1", "--locked", "--manifest-path"])
            .arg(manifest).arg("--filter-platform").arg(target);
        options.configure_cargo(&mut cargo, &[]);
        let metadata: Value = serde_json::from_str(&output(&mut cargo)?)
            .map_err(|e| format!("invalid Cargo metadata: {e}"))?;
        Ok(Self { packages: selected_packages(&metadata, manifest)? })
    }
    pub fn contains(&self, name: &str) -> bool { self.packages.contains(name) }
}
pub fn host_target(rustc: &mut Command) -> Result<String> {
    output(rustc.arg("-vV"))?.lines().find_map(|line| line.strip_prefix("host: "))
        .map(str::to_owned).ok_or_else(|| "rustc did not report a host target".into())
}
fn selected_packages(metadata: &Value, manifest: &Path) -> Result<BTreeSet<String>> {
    let requested = manifest.canonicalize().map_err(|e| e.to_string())?;
    let packages = metadata["packages"].as_array().ok_or("missing Cargo packages")?;
    let root_id = packages.iter().find(|p| p["manifest_path"].as_str().is_some_and(|path| {
        Path::new(path).canonicalize().is_ok_and(|path| path == requested)
    })).and_then(|p| p["id"].as_str()).ok_or("application is not a Cargo package")?;
    let identities: BTreeMap<&str, &str> = packages.iter().map(|p| Ok((
        p["id"].as_str().ok_or("missing package ID")?,
        p["name"].as_str().ok_or("missing package name")?,
    ))).collect::<Result<_>>()?;
    let nodes: BTreeMap<&str, &Value> = metadata["resolve"]["nodes"].as_array()
        .ok_or("missing Cargo resolve nodes")?.iter()
        .map(|node| Ok((node["id"].as_str().ok_or("missing node ID")?, node)))
        .collect::<Result<_>>()?;
    let mut pending = vec![root_id];
    let mut visited = BTreeSet::new();
    let mut selected = BTreeSet::new();
    while let Some(id) = pending.pop() {
        if !visited.insert(id) { continue; }
        selected.insert((*identities.get(id).ok_or("unknown Cargo package")?).to_owned());
        let node = nodes.get(id).ok_or("missing resolved Cargo node")?;
        for dependency in node["deps"].as_array().ok_or("missing node dependencies")? {
            // With --filter-platform, Cargo removes inactive target dependencies.
            // Disabled optional features likewise have no normal dependency edge.
            let kinds = dependency["dep_kinds"].as_array().ok_or("missing dependency kinds")?;
            if kinds.iter().any(|kind| kind["kind"].is_null()) {
                pending.push(dependency["pkg"].as_str().ok_or("missing dependency ID")?);
            }
        }
    }
    Ok(selected)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn excludes_unselected_workspace_packages_and_build_only_edges() {
        let d = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let manifest = d.0.join("app/Cargo.toml");
        std::fs::create_dir_all(manifest.parent().unwrap()).unwrap();
        std::fs::write(&manifest, "[package]\nname = \"app\"\n").unwrap();
        let data = json!({
            "packages": [
                {"id":"app","name":"app","manifest_path":manifest},
                {"id":"detector","name":"nxrs-detector","manifest_path":d.0.join("detector/Cargo.toml")},
                {"id":"unused","name":"nxrs-imu","manifest_path":d.0.join("imu/Cargo.toml")}
            ],
            "resolve":{"nodes":[
                {"id":"app","deps":[{"pkg":"detector","dep_kinds":[{"kind":null}]}]},
                {"id":"detector","deps":[]},
                {"id":"unused","deps":[]}
            ]}
        });
        assert_eq!(selected_packages(&data, &manifest).unwrap(),
            BTreeSet::from(["app".to_owned(), "nxrs-detector".to_owned()]));
        let mut dev_only = data;
        dev_only["resolve"]["nodes"][0]["deps"][0]["dep_kinds"] = json!([{"kind":"dev"}]);
        assert_eq!(selected_packages(&dev_only, &manifest).unwrap(),
            BTreeSet::from(["app".to_owned()]));
    }
}
