//! Build admission and evidence for an application descriptor and resolved profile.
use crate::{
    hash,
    profile::{manifest, Profile, CAPABILITIES},
    Result,
};
use serde_json::{json, Value};
use std::path::Path;

pub fn validate(root: &Path, path: &Path, profile: &Profile, os: &str) -> Result<Value> {
    let path = path.canonicalize().map_err(|e| e.to_string())?;
    let app = manifest(&path)?;
    let hal = root.join("hal").canonicalize().map_err(|e| e.to_string())?;
    if let Some(deps) = app["dependencies"].as_object() {
        for (alias, dep) in deps {
            let Some(p) = dep["path"].as_str() else {
                continue;
            };
            let target = path.parent().unwrap().join(p).canonicalize().map_err(|e| {
                format!("application dependency {alias} selects an unavailable path: {e}")
            })?;
            if let Ok(relative) = target.strip_prefix(&hal) {
                let name = relative.to_str().ok_or("invalid HAL dependency path")?;
                if relative.components().count() != 1
                    || (!CAPABILITIES.contains(&name) && name != "common")
                {
                    return Err(format!(
                        "application dependency {alias} selects a concrete HAL implementation"
                    ));
                }
                if dep["features"].as_array().is_some_and(|v| {
                    v.iter().any(|v| {
                        v.as_str().is_some_and(|s| {
                            [
                                "native", "mock", "nuttx", "std", "linux", "macos", "windows",
                            ]
                            .contains(&s)
                        })
                    })
                }) {
                    return Err(format!(
                        "application dependency {alias} selects a provider feature; choose one platform"
                    ));
                }
            }
        }
    }
    profile.validate_target(os)?;
    if let Some(required) = app.pointer("/package/metadata/nxrs/required-capabilities") {
        let required = required
            .as_array()
            .ok_or("required-capabilities must list known HAL capabilities")?;
        for c in required {
            let c = c
                .as_str()
                .filter(|s| CAPABILITIES.contains(s))
                .ok_or("required-capabilities must list known HAL capabilities")?;
            if profile.binding(c) == "none" {
                return Err(format!(
                    "{}: missing required capability {c}",
                    profile.name()
                ));
            }
        }
    }
    let providers: Vec<_> = profile.data["hal"]
        .as_object()
        .unwrap()
        .iter()
        .filter(|(_, v)| v.as_str() != Some("none"))
        .map(|(c, p)| json!({"capability":c,"implementation":p,"package":format!("nxrs-{c}")}))
        .collect();
    let sources = profile
        .sources
        .iter()
        .map(|p| Ok(json!({"path":p,"sha256":hash(p)?})))
        .collect::<Result<Vec<_>>>()?;
    let detector = profile.detector()?.map(|v| {
        let mut d = json!({"model":v.model,"stack-bytes":v.stack_bytes,"sample-ms":v.sample_ms});
        if let Some(c) = v.compute_core {
            d["compute-core"] = json!(c)
        }
        d
    });
    Ok(
        json!({"app_manifest":path,"app_package":app["package"]["name"],"platform":profile.name(),"target_os":os,"detector":detector,"model_features":null,"dependency_resolution":{"status":"pending","reason":"requires the build's Cargo features, target and compiler"},"providers":providers,"profiles":sources}),
    )
}
