//! Resolve product resources to the selected BSP's NuttX settings and VFS paths.
//! NuttX owns pin encoding, controller initialization and endpoint registration.
use crate::{read, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct Board {
    resources: BTreeMap<String, Binding>,
    // Pin settings and reservations use this BSP's native numbering.
    // No port arithmetic, mux encoding or chip pin catalogue belongs here.
    #[serde(default)]
    pin_namespace: String,
    #[serde(default)]
    kconfig: BTreeMap<String, String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct Binding {
    kind: String,
    device: String,
    #[serde(default)]
    kconfig: BTreeMap<String, String>,
    #[serde(default)]
    pin_settings: BTreeSet<String>,
    #[serde(default)]
    reserved_pins: Vec<u64>,
    #[serde(default)]
    values: BTreeMap<String, String>,
    #[serde(default)]
    properties: BTreeMap<String, Value>,
    #[serde(default)]
    chip_selects: Option<Vec<u64>>,
}

pub(crate) fn resolve(data: &mut Value, path: &Path) -> Result<()> {
    let board: Board =
        toml::from_str(&read(path)?).map_err(|error| format!("{}: {error}", path.display()))?;
    if board.pin_namespace.is_empty()
        && board
            .resources
            .values()
            .any(|b| !b.pin_settings.is_empty() || !b.reserved_pins.is_empty())
    {
        return Err(format!(
            "{}: pin settings require an explicit native pin-namespace",
            path.display()
        ));
    }
    let mut settings = board.kconfig;
    let mut selections = BTreeMap::new();
    let mut pin_owners = BTreeMap::new();
    let devices = data.get("devices").cloned().unwrap_or(json!({}));
    let resources = data["resources"]
        .as_object_mut()
        .ok_or("resources must be a table")?;
    for (id, raw) in resources {
        let record = raw.as_object_mut().ok_or("resource must be a table")?;
        let selected = record.remove("binding").ok_or_else(|| {
            format!("resources.{id}: select a BSP binding instead of a raw device path")
        })?;
        let name = selected
            .as_str()
            .ok_or("resource binding must be a string")?;
        let binding = board.resources.get(name).ok_or_else(|| {
            format!(
                "resources.{id}: unsupported BSP resource {name} in {}",
                path.display()
            )
        })?;
        if let Some(selects) = &binding.chip_selects {
            for (device_id, device) in devices.as_object().ok_or("devices must be a table")? {
                if device["bus"] == *id
                    && !device["chip-select"]
                        .as_u64()
                        .is_some_and(|cs| selects.contains(&cs))
                {
                    return Err(format!("devices.{device_id}: BSP resource {name} only supports chip-selects {selects:?}"));
                }
            }
        }
        if record.contains_key("device") || record.contains_key("kind") {
            return Err(format!(
                "resources.{id}: binding owns kind and device; remove the duplicate fields"
            ));
        }
        if ["pins", "pinctrl", "gpio"]
            .iter()
            .any(|field| record.contains_key(*field))
        {
            return Err(format!("resources.{id}: replace pins/pinctrl/gpio with the binding's native kconfig pin settings"));
        }
        let pins = record.remove("kconfig").unwrap_or(json!({}));
        let pins = pins.as_object().ok_or("resource kconfig must be a table")?;
        if pins.keys().cloned().collect::<BTreeSet<_>>() != binding.pin_settings {
            return Err(format!(
                "resources.{id}.kconfig: expected exactly the native pin settings {:?}",
                binding.pin_settings
            ));
        }
        for pin in &binding.reserved_pins {
            claim_pin(
                &mut pin_owners,
                *pin,
                &format!("{name} BSP reservation"),
                id,
                &board.pin_namespace,
            )?;
        }
        for (symbol, value) in pins {
            let pin = value.as_u64().ok_or_else(|| {
                format!("resources.{id}.kconfig.{symbol}: expected an unsigned native pin number")
            })?;
            claim_pin(&mut pin_owners, pin, symbol, id, &board.pin_namespace)?;
            insert(&mut settings, symbol, &pin.to_string(), id)?;
        }
        for (field, symbol) in &binding.values {
            let value = record
                .get(field)
                .and_then(Value::as_u64)
                .ok_or_else(|| format!("resources.{id}.{field}: expected an unsigned integer"))?;
            insert(&mut settings, symbol, &value.to_string(), id)?;
        }
        for (symbol, value) in &binding.kconfig {
            insert(&mut settings, symbol, value, id)?;
        }
        for (field, value) in &binding.properties {
            if record
                .get(field)
                .is_some_and(|requested| requested != value)
            {
                return Err(format!(
                    "resources.{id}.{field}: BSP binding requires {value}"
                ));
            }
            record.insert(field.clone(), value.clone());
        }
        record.insert("kind".into(), json!(binding.kind));
        record.insert("device".into(), json!(binding.device));
        selections.insert(
            id.clone(),
            json!({"binding": name, "device": binding.device, "kconfig": pins}),
        );
    }
    let config = data
        .as_object_mut()
        .ok_or("profile must be a table")?
        .entry("kconfig")
        .or_insert(json!({}));
    merge_config(config, &settings)?;
    data["resolved-nuttx-resources"] = json!({"selections": selections, "kconfig": settings, "pin-namespace": board.pin_namespace});
    Ok(())
}

fn claim_pin(
    owners: &mut BTreeMap<u64, String>,
    pin: u64,
    owner: &str,
    id: &str,
    namespace: &str,
) -> Result<()> {
    if let Some(previous) = owners.insert(pin, owner.into()) {
        if previous != owner {
            return Err(format!(
                "resources.{id}: {namespace} pin {pin} is assigned to both {previous} and {owner}"
            ));
        }
    }
    Ok(())
}

fn insert(settings: &mut BTreeMap<String, String>, key: &str, value: &str, id: &str) -> Result<()> {
    if let Some(previous) = settings.insert(key.into(), value.into()) {
        if previous != value {
            return Err(format!(
                "resources.{id}: conflicting BSP setting {key}: {previous} versus {value}"
            ));
        }
    }
    Ok(())
}

fn assignment(line: &str) -> Option<(&str, &str)> {
    if let Some(symbol) = line
        .strip_prefix("# CONFIG_")
        .and_then(|s| s.strip_suffix(" is not set"))
    {
        Some((symbol, "n"))
    } else {
        line.strip_prefix("CONFIG_").unwrap_or(line).split_once('=')
    }
}

fn check(settings: &BTreeMap<String, String>, key: &str, value: &str) -> Result<()> {
    if let Some(required) = settings.get(key) {
        if required != value {
            return Err(format!("Kconfig {key}={value} conflicts with BSP resource requirement {key}={required}; change the resource selection"));
        }
    }
    Ok(())
}

fn merge_config(config: &mut Value, settings: &BTreeMap<String, String>) -> Result<()> {
    let config = config.as_object_mut().ok_or("kconfig must be a table")?;
    for field in ["enable", "disable", "set", "require"] {
        let list = config
            .entry(field)
            .or_insert(json!([]))
            .as_array_mut()
            .ok_or("invalid Kconfig list")?;
        for entry in list.iter() {
            let entry = entry.as_str().ok_or("Kconfig entries must be strings")?;
            let (key, value) = match field {
                "enable" => (entry, "y"),
                "disable" => (entry, "n"),
                _ => assignment(entry).ok_or("invalid Kconfig assignment")?,
            };
            check(settings, key, value)?;
        }
        for (key, value) in settings {
            let entry = match (field, value.as_str()) {
                ("enable", "y") | ("disable", "n") => Some(key.clone()),
                // Kconfig omits disabled symbols with unmet dependencies.
                // validate_config checks these as absent or explicitly disabled.
                ("require", "n") => None,
                ("require", _) => Some(format!("CONFIG_{key}={value}")),
                ("set", value) if !["y", "n"].contains(&value) => Some(format!("{key}={value}")),
                _ => None,
            };
            if let Some(entry) = entry {
                let entry = json!(entry);
                if !list.contains(&entry) {
                    list.push(entry);
                }
            }
        }
    }
    Ok(())
}

/// Qualification overrides may add diagnostics, but cannot contradict resource wiring.
pub fn validate_overrides(data: &Value, enable: &[String], disable: &[String]) -> Result<()> {
    let Some(settings) = data.pointer("/resolved-nuttx-resources/kconfig") else {
        return Ok(());
    };
    let settings = serde_json::from_value(settings.clone())
        .map_err(|e| format!("invalid BSP settings: {e}"))?;
    for (symbols, value) in [(enable, "y"), (disable, "n")] {
        for symbol in symbols {
            check(&settings, symbol, value)?;
        }
    }
    Ok(())
}

/// Check the effective configuration after olddefconfig, including disabled dependencies.
pub fn validate_config(data: &Value, config: &str) -> Result<()> {
    let Some(settings) = data
        .pointer("/resolved-nuttx-resources/kconfig")
        .and_then(Value::as_object)
    else {
        return Ok(());
    };
    let actual: BTreeMap<_, _> = config.lines().filter_map(assignment).collect();
    for (key, expected) in settings {
        let expected = expected.as_str().ok_or("invalid BSP setting")?;
        if actual.get(key.as_str()).copied().unwrap_or("n") != expected {
            return Err(format!(
                "unresolved BSP resource requirement: CONFIG_{key}={expected}"
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        profile::{manifest, Profile},
        TempDir,
    };
    use std::fs;

    fn root() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap()
    }
    fn raw() -> Value {
        manifest(&root().join("tests/profiles/esp32s3-devkit-imu-qualification.toml"))
            .unwrap()
    }
    fn resolve_devkit(value: &mut Value) -> Result<()> {
        resolve(
            value,
            &root().join("platform/nuttx/resources/esp32s3-devkit.toml"),
        )
    }

    #[test]
    fn overlay_updates_native_pins_path_and_bringup_without_stale_settings() {
        let tmp = TempDir::new(&std::env::temp_dir()).unwrap();
        let overlay = tmp.0.join("bus.toml");
        let base = Profile::resolve(&root(), "esp32s3-devkit-imu-qualification").unwrap();
        // No catalogue entry for the complete SDA/SCL combination is needed.
        fs::write(&overlay, "[resources.sensor_bus]\nkconfig = { ESP32S3_I2C0_SDAPIN = 8, ESP32S3_I2C0_SCLPIN = 11 }\n").unwrap();
        let changed = Profile::resolve_with_overlays(
            &root(),
            "esp32s3-devkit-imu-qualification",
            std::slice::from_ref(&overlay),
        )
        .unwrap();
        assert_eq!(base.composition().unwrap(), changed.composition().unwrap());
        assert_eq!(
            changed.data["resolved-nuttx-resources"]["kconfig"]["ESP32S3_I2C0_SCLPIN"],
            "11"
        );
        assert_ne!(
            base.json().unwrap()["resolved-composition"]["semantic-digest"],
            changed.json().unwrap()["resolved-composition"]["semantic-digest"]
        );
        fs::write(&overlay, "remove-resources = [\"imu_irq\"]\n[resources.sensor_bus]\nbinding = \"i2c1\"\nkconfig = { ESP32S3_I2C1_SDAPIN = 38, ESP32S3_I2C1_SCLPIN = 39 }\nfrequency = 400000\n[devices.imu.wake]\nkind = \"periodic\"\n").unwrap();
        let p =
            Profile::resolve_with_overlays(&root(), "esp32s3-devkit-imu-qualification", &[overlay])
                .unwrap();
        assert_eq!(
            p.composition().unwrap().unwrap().imu().unwrap().path,
            "/dev/i2c1"
        );
        let config = &p.data["resolved-nuttx-resources"]["kconfig"];
        assert_eq!(config["ESP32S3_I2C1"], "y");
        assert_eq!(config["ESP32S3_I2C1_SDAPIN"], "38");
        assert_eq!(config["ESP32S3_I2C1_SCLPIN"], "39");
        assert!(config.get("ESP32S3_I2C0").is_none());
        assert!(config.get("DEV_GPIO").is_none());
        assert!(p.data["kconfig"]["require"]
            .as_array()
            .unwrap()
            .contains(&json!("CONFIG_ESP32S3_I2C1_SDAPIN=38")));
        assert!(p
            .source_paths()
            .any(|p| p.ends_with("resources/esp32s3-devkit.toml")));
    }

    #[test]
    fn rejects_incomplete_foreign_or_conflicting_native_settings() {
        for (pointer, value, message) in [
            (
                "/resources/sensor_bus/binding",
                json!("missing"),
                "unsupported BSP resource",
            ),
            (
                "/resources/sensor_bus/device",
                json!("/dev/i2c1"),
                "binding owns kind and device",
            ),
            (
                "/resources/sensor_bus/pins",
                json!({"sda":8,"scl":9}),
                "replace pins/pinctrl/gpio",
            ),
            (
                "/resources/sensor_bus/pinctrl",
                json!("gpio8_gpio9"),
                "replace pins/pinctrl/gpio",
            ),
            (
                "/resources/imu_irq/gpio",
                json!({"controller":"gpio0","pin":16}),
                "replace pins/pinctrl/gpio",
            ),
            (
                "/resources/sensor_bus/kconfig",
                json!({"ESP32S3_I2C0_SDAPIN":8}),
                "expected exactly",
            ),
            (
                "/resources/sensor_bus/kconfig",
                json!({"ESP32S3_I2C1_SDAPIN":8,"ESP32S3_I2C1_SCLPIN":9}),
                "expected exactly",
            ),
            (
                "/resources/sensor_bus/kconfig/ESP32S3_I2C0_SCLPIN",
                json!(-1),
                "unsigned native pin",
            ),
            (
                "/resources/sensor_bus/kconfig/ESP32S3_I2C0_SCLPIN",
                json!("PB9"),
                "unsigned native pin",
            ),
            (
                "/resources/sensor_bus/kconfig/DEBUG_FEATURES",
                json!(1),
                "expected exactly",
            ),
            (
                "/resources/imu_irq/trigger",
                json!("falling"),
                "BSP binding requires",
            ),
            (
                "/kconfig/disable",
                json!(["I2C_DRIVER"]),
                "conflicts with BSP",
            ),
            (
                "/kconfig/set",
                json!(["ESP32S3_I2C0_SDAPIN=7"]),
                "conflicts with BSP",
            ),
            (
                "/kconfig/require",
                json!(["CONFIG_ESP32S3_I2C0=n"]),
                "conflicts with BSP",
            ),
        ] {
            let mut data = raw();
            let (parent, key) = pointer.rsplit_once('/').unwrap();
            data.pointer_mut(parent).unwrap()[key] = value;
            let error = resolve_devkit(&mut data).unwrap_err();
            assert!(error.contains(message), "{pointer}: {error}");
        }
        for (resource, field, error) in [
            ("sensor_bus", "binding", "select a BSP binding"),
            ("sensor_bus", "kconfig", "expected exactly"),
            ("imu_irq", "kconfig", "expected exactly"),
        ] {
            let mut data = raw();
            data["resources"][resource]
                .as_object_mut()
                .unwrap()
                .remove(field);
            assert!(resolve_devkit(&mut data).unwrap_err().contains(error));
        }
    }

    #[test]
    fn native_pin_conflicts_cover_bus_signals_irq_and_bsp_reservations() {
        for (resource, symbol, pin) in [
            ("sensor_bus", "ESP32S3_I2C0_SCLPIN", 8),
            ("sensor_bus", "ESP32S3_I2C0_SCLPIN", 16),
            ("imu_irq", "ESP32S3_DEVKIT_GPIO_IRQ_PIN", 15),
        ] {
            let mut data = raw();
            data["resources"][resource]["kconfig"][symbol] = json!(pin);
            assert!(resolve_devkit(&mut data)
                .unwrap_err()
                .contains(&format!("esp32s3-gpio pin {pin} is assigned")));
        }
        let mut data = raw();
        data["resources"]["same_bus"] = data["resources"]["sensor_bus"].clone();
        resolve_devkit(&mut data).unwrap(); // Reusing one physical bus is permitted.
        let mut data = raw();
        data["resources"]["same_bus"] = data["resources"]["sensor_bus"].clone();
        data["resources"]["same_bus"]["kconfig"]["ESP32S3_I2C0_SDAPIN"] = json!(10);
        assert!(resolve_devkit(&mut data)
            .unwrap_err()
            .contains("conflicting BSP setting"));

        let tmp = TempDir::new(&std::env::temp_dir()).unwrap();
        let path = tmp.0.join("missing-namespace.toml");
        let bsp = read(&root().join("platform/nuttx/resources/esp32s3-devkit.toml")).unwrap();
        fs::write(&path, bsp.replace("pin-namespace = \"esp32s3-gpio\"", "")).unwrap();
        assert!(resolve(&mut raw(), &path)
            .unwrap_err()
            .contains("explicit native pin-namespace"));
    }

    #[test]
    fn actual_config_and_diagnostic_overrides_must_preserve_resource_contract() {
        let mut data = raw();
        resolve_devkit(&mut data).unwrap();
        let config = data["resolved-nuttx-resources"]["kconfig"]
            .as_object()
            .unwrap()
            .iter()
            .filter(|(_, v)| *v != "n")
            .map(|(k, v)| format!("CONFIG_{k}={}\n", v.as_str().unwrap()))
            .collect::<String>();
        validate_config(&data, &config).unwrap();
        assert!(validate_config(&data, &config.replace("CONFIG_I2C_DRIVER=y\n", "")).is_err());
        assert!(validate_config(
            &data,
            &config.replace(
                "CONFIG_ESP32S3_I2C0_SDAPIN=8",
                "CONFIG_ESP32S3_I2C0_SDAPIN=1"
            )
        )
        .is_err());
        assert!(validate_config(&data, &(config + "CONFIG_I2C_POLLED=y\n")).is_err());
        validate_overrides(&data, &["DEBUG_I2C_ERROR".into()], &[]).unwrap();
        assert!(validate_overrides(&data, &[], &["I2C_DRIVER".into()]).is_err());
    }

    #[test]
    fn uart_numbering_and_baud_follow_the_same_bsp_contract() {
        let mut data =
            manifest(&root().join("platform/cores3/profile.toml")).unwrap();
        data["resources"]["receiver_uart"]["baud"] = json!(115200);
        resolve(
            &mut data,
            &root().join("platform/nuttx/resources/esp32s3-m5-cores3.toml"),
        )
        .unwrap();
        assert_eq!(data["resources"]["receiver_uart"]["device"], "/dev/ttyS1");
        let settings = &data["resolved-nuttx-resources"]["kconfig"];
        assert_eq!(settings["UART1_BAUD"], "115200");
        assert_eq!(settings["ESP32S3_UART0"], "y");
        assert!(validate_overrides(&data, &["UART1_SERIAL_CONSOLE".into()], &[]).is_err());
        assert!(validate_overrides(&data, &[], &["ESP32S3_UART0".into()]).is_err());
    }

    #[test]
    fn spi_requires_a_bsp_that_registers_it_and_its_supported_chip_select() {
        let overlay = root().join("tests/nuttx-std/imu-qualification/overlays/bmi270-spi.toml");
        let devkit = Profile::resolve_with_overlays(
            &root(),
            "esp32s3-devkit-imu-qualification",
            std::slice::from_ref(&overlay),
        )
        .unwrap();
        assert_eq!(
            devkit.composition().unwrap().unwrap().imu().unwrap().path,
            "/dev/spi2"
        );
        assert_eq!(
            devkit.data["resolved-nuttx-resources"]["kconfig"]["SPI_DRIVER"],
            "y"
        );
        assert!(
            Profile::resolve_with_overlays(&root(), "m5stack-cores3-gnss", &[overlay])
                .unwrap_err()
                .contains("unsupported BSP resource spi2")
        );
        let mut data = raw();
        data["resources"]["sensor_bus"] = json!({"binding":"spi2", "kconfig":{
            "ESP32S3_SPI2_CLKPIN":4,"ESP32S3_SPI2_MOSIPIN":5,"ESP32S3_SPI2_MISOPIN":6,"ESP32S3_SPI2_CSPIN":10}, "frequency":4000000});
        data["devices"]["imu"]["chip-select"] = json!(1);
        assert!(resolve_devkit(&mut data)
            .unwrap_err()
            .contains("only supports chip-selects [0]"));
    }
}
