//! JSON bridge for existing qualification runners. All configuration policy stays in Rust.
use crate::{profile::Profile, Result};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
pub fn query(request: Value) -> Result<Value> {
    let root = PathBuf::from(
        request["root"]
            .as_str()
            .ok_or("inspection requires workspace root")?,
    );
    let action = request["action"]
        .as_str()
        .ok_or("inspection requires action")?;
    if action == "capabilities" {
        return Ok(json!(crate::profile::CAPABILITIES));
    }
    if action == "resolve" {
        return Profile::resolve(
            &root,
            request["name"].as_str().ok_or("missing profile name")?,
        )?
        .json();
    }
    let mut data = request["data"].clone();
    if !data.is_object() {
        return Err("profile inspection requires an object".into());
    }
    let sources = data
        .as_object_mut()
        .unwrap()
        .remove("sources")
        .unwrap_or(json!([]));
    let sources = sources
        .as_array()
        .ok_or("profile sources must be an array")?
        .iter()
        .map(|v| {
            v.as_str()
                .map(PathBuf::from)
                .ok_or("invalid profile source".into())
        })
        .collect::<Result<Vec<_>>>()?;
    let p = Profile::from_data(data, sources);
    match action {
        "validate-target" => {
            p.validate_target(request["os"].as_str().ok_or("missing target OS")?)?;
            Ok(Value::Null)
        }
        "cargo" => Ok(json!(p.cargo_output(
            &root,
            request["capability"].as_str().ok_or("missing capability")?,
            request["os"].as_str().ok_or("missing target OS")?
        )?)),
        "detector" => Ok(p
            .detector()?
            .map(|d| {
                let mut v =
                    json!({"model":d.model,"stack-bytes":d.stack_bytes,"sample-ms":d.sample_ms});
                if let Some(c) = d.compute_core {
                    v["compute-core"] = json!(c);
                }
                v
            })
            .unwrap_or(Value::Null)),
        "network" => Ok(json!(p.network_initialization()?)),
        "composition" => Ok(p
            .composition()?
            .map_or(Value::Null, |graph| p.composition_report(&graph))),
        "scheduling" | "console" => {
            p.validate_resources()?;
            if action == "console" {
                return Ok(p.data["console"]
                    .get("device")
                    .cloned()
                    .unwrap_or(json!("")));
            }
            Ok(p.data.get("scheduling").cloned().unwrap_or(json!({})))
        }
        "link-arguments" => {
            let args = request["args"]
                .as_array()
                .ok_or("link arguments must be an array")?
                .iter()
                .map(|a| {
                    a.as_str()
                        .map(str::to_owned)
                        .ok_or("invalid link argument".into())
                })
                .collect::<Result<Vec<_>>>()?;
            Ok(json!(crate::linker::arguments(&args)?))
        }
        "link-prune" => {
            crate::linker::prune(
                Path::new(request["binary"].as_str().ok_or("missing binary")?),
                request["prefix"].as_str().ok_or("missing tool prefix")?,
                Path::new(
                    request["evidence"]
                        .as_str()
                        .ok_or("missing evidence directory")?,
                ),
            )?;
            Ok(Value::Null)
        }
        "kconfig" => {
            let values = request["values"]
                .as_object()
                .ok_or("Kconfig values must be an object")?
                .iter()
                .map(|(k, v)| {
                    Ok((
                        k.clone(),
                        v.as_str()
                            .ok_or("Kconfig values must be strings")?
                            .to_owned(),
                    ))
                })
                .collect::<Result<std::collections::BTreeMap<_, _>>>()?;
            let path = Path::new(request["path"].as_str().ok_or("missing Kconfig path")?);
            crate::nuttx::configure(path, &values)?;
            let required = request["requirements"]
                .as_array()
                .ok_or("requirements must be an array")?
                .iter()
                .map(|v| {
                    v.as_str()
                        .map(str::to_owned)
                        .ok_or("invalid requirement".into())
                })
                .collect::<Result<Vec<_>>>()?;
            crate::nuttx::required(&crate::read(path)?, &required, "")?;
            Ok(Value::Null)
        }
        "release-version" => Ok(json!(crate::source::release_version(
            &root,
            request["revision"].as_str().ok_or("missing revision")?
        )?)),
        "stamp-version" => {
            let revision = request["revision"].as_str().ok_or("missing revision")?;
            crate::source::stamp(
                &root,
                Path::new(request["source"].as_str().ok_or("missing source archive")?),
                revision,
            )?;
            Ok(json!(crate::source::release_version(&root, revision)?))
        }
        "isolated-hal" => Ok(json!(crate::nuttx::isolated_hal(
            Path::new(request["nuttx"].as_str().ok_or("missing NuttX build")?),
            Path::new(request["hal"].as_str().ok_or("missing HAL build")?)
        )?)),
        "source-revision" => Ok(json!(crate::source::revision(
            &root,
            request["name"].as_str().ok_or("missing dependency name")?
        )?)),
        "verify-inventory" => {
            crate::source::verify_inventory(
                Path::new(request["ledger"].as_str().ok_or("missing SDK ledger")?),
                Path::new(
                    request["destination"]
                        .as_str()
                        .ok_or("missing SDK destination")?,
                ),
            )?;
            Ok(Value::Null)
        }
        "layout" => {
            crate::boot::Layout::parse(&p.data["mcuboot"])?;
            Ok(p.data["mcuboot"].clone())
        }
        "image-class" => {
            crate::boot::image_class(&p)?;
            Ok(p.data["image-class"].clone())
        }
        "detector-context" => {
            crate::detector_sdk::qualify_context(Path::new(
                request["include"]
                    .as_str()
                    .ok_or("missing target include")?,
            ))?;
            Ok(Value::Null)
        }
        "protected" => Ok(json!(crate::boot::protected(&p)?)),
        "watchdog" => Ok(json!(crate::boot::watchdog_timeout(&p)?)),
        "init" => Ok(json!(crate::boot::init_config(
            &p,
            request["command"].as_str().ok_or("missing command")?,
            request["priority"].as_u64().ok_or("invalid priority")?,
            request["stack"].as_u64().ok_or("invalid stack")?
        )?)),
        "factory" => {
            let path = |key: &str| -> Result<&Path> {
                Ok(Path::new(
                    request[key]
                        .as_str()
                        .ok_or_else(|| format!("missing {key}"))?,
                ))
            };
            crate::factory::prepare(
                &p,
                crate::factory::Inputs {
                    boot: path("boot-build")?,
                    application: path("application")?,
                    rom_root: path("rom-root")?,
                    app_root: path("app-root")?,
                    flash_key: path("flash-key")?,
                },
                path("output")?,
            )?;
            Ok(Value::Null)
        }
        "rom-sign" => Ok(json!(crate::boot::sign_rom(
            Path::new(request["input"].as_str().ok_or("missing input")?),
            Path::new(request["output"].as_str().ok_or("missing output")?),
            Path::new(request["key"].as_str().ok_or("missing key")?),
            Path::new(request["key-root"].as_str().ok_or("missing public root")?)
        )?)),
        "sign" => {
            let signing = crate::boot::Signing {
                key: PathBuf::from(request["key"].as_str().ok_or("missing signing key")?),
                root: PathBuf::from(
                    request["key-root"]
                        .as_str()
                        .ok_or("missing independent root")?,
                ),
                version: request["version"].as_str().ok_or("missing version")?.into(),
                counter: request["counter"]
                    .as_u64()
                    .and_then(|n| u32::try_from(n).ok())
                    .ok_or("invalid unsigned security counter")?,
            };
            signing.sign(
                &p,
                Path::new(request["input"].as_str().ok_or("missing input")?),
                Path::new(request["output"].as_str().ok_or("missing output")?),
                request["confirm"].as_bool().ok_or("invalid confirm flag")?,
            )?;
            Ok(Value::Null)
        }
        "boot-application" => Ok(json!(crate::boot::application_config(&p)?)),
        "bootloader" => Ok(json!(crate::boot::bootloader_config(
            &p,
            Path::new(
                request["key-root"]
                    .as_str()
                    .ok_or("missing public root path")?
            )
        )?)),
        _ => Err("unknown inspection action".into()),
    }
}
