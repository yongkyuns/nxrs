//! Build admission and evidence for an application descriptor and resolved profile.
use crate::{
    profile::{manifest, Profile, CAPABILITIES},
    Result,
};
use serde_json::{json, Value};
use std::path::Path;

/// Validate application metadata before optional fields can silently fall back.
pub(crate) fn validate_metadata(app: &Value) -> Result<()> {
    if let Some(nxrs) = app.pointer("/package/metadata/nxrs") {
        let fields = nxrs
            .as_object()
            .ok_or("package.metadata.nxrs must be a table")?;
        if let Some(key) = fields.keys().find(|key| {
            !matches!(
                key.as_str(),
                "firmware" | "required-capabilities" | "qualification-only"
            )
        }) {
            return Err(format!(
                "unknown application metadata field package.metadata.nxrs.{key}"
            ));
        }
        if fields
            .get("qualification-only")
            .is_some_and(|value| !value.is_boolean())
        {
            return Err("package.metadata.nxrs.qualification-only must be a boolean".into());
        }
    }
    Ok(())
}

pub fn validate(root: &Path, path: &Path, profile: &Profile, os: &str) -> Result<Value> {
    let path = path.canonicalize().map_err(|e| e.to_string())?;
    let app = manifest(&path)?;
    validate_metadata(&app)?;
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
    let composition = profile.composition()?;
    let qualification = composition
        .as_ref()
        .is_some_and(|graph| graph.mode() == nxrs_build_support::composition::Mode::Qualification);
    if qualification
        && app.pointer("/package/metadata/nxrs/qualification-only") != Some(&Value::Bool(true))
    {
        return Err(
            "qualification composition requires package.metadata.nxrs.qualification-only = true"
                .into(),
        );
    }
    let protected = match profile.data.pointer("/build/protected-runtime") {
        None => false,
        Some(value) => value
            .as_bool()
            .ok_or("build.protected-runtime must be a boolean")?,
    };
    if protected
        && (!qualification
            || os != "nuttx"
            || profile.data["target"] != "xtensa-esp32s3-nuttx"
            || !profile.data.get("init").is_some_and(Value::is_object)
            || profile.data.get("mcuboot").is_some())
    {
        return Err(
            "protected runtime requires unsigned, direct-init ESP32-S3 qualification".into(),
        );
    }
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
    let sources = profile.source_hashes()?;
    let detector = profile.detector()?.map(|v| {
        let mut d = json!({"model":v.model,"stack-bytes":v.stack_bytes,"sample-ms":v.sample_ms});
        if let Some(c) = v.compute_core {
            d["compute-core"] = json!(c)
        }
        d
    });
    let mut evidence = json!({"app_manifest":path,"app_package":app["package"]["name"],"platform":profile.name(),"target_os":os,"detector":detector,"model_features":null,"implementation_features":null,"preparation":null,"dependency_resolution":{"status":"pending","reason":"requires the build's Cargo features, target and compiler"},"providers":providers,"profiles":sources});
    if os == "nuttx" {
        evidence["nuttx-build-mode"] = json!(if protected { "protected" } else { "flat" });
    }
    if let Some(graph) = composition {
        evidence["resolved-composition"] = profile.composition_report(&graph);
    }
    Ok(evidence)
}
