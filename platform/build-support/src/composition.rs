//! Typed, build-time validation for versioned peripheral composition profiles.
use crate::Result;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

/// Checked-in capability-owned binding catalog embedded in the resolver.
pub const BINDING_SCHEMA_PATH: &str = "hal/imu/bindings/bmi270-i2c.toml";
const CATALOG_SOURCE: &str = include_str!("../../../hal/imu/bindings/bmi270-i2c.toml");

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Hardware,
    Simulation,
    Qualification,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ImuModel {
    Bmi270,
}

/// Normalized graph. Maps serialize in key order and endpoint paths are normalized.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Composition {
    version: u32,
    mode: Mode,
    resources: BTreeMap<String, Resource>,
    devices: BTreeMap<String, Device>,
    roles: BTreeMap<String, String>,
    stack_bytes: u64,
}

/// Borrowed view of the resolved default IMU device.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImuBinding<'a> {
    pub model: ImuModel,
    pub path: &'a str,
    pub address: u8,
    pub frequency: u32,
    pub irq_path: Option<&'a str>,
    pub stack_bytes: u64,
}

impl Composition {
    pub fn imu(&self) -> Option<ImuBinding<'_>> {
        let device_id = self.roles.get("imu")?;
        let device = self.devices.get(device_id)?;
        let Resource::I2c {
            device: path,
            frequency,
        } = self.resources.get(&device.bus)?
        else {
            return None;
        };
        let irq_path = match &device.wake {
            Wake::Periodic {} => None,
            Wake::Interrupt { resource } => match self.resources.get(resource)? {
                Resource::GpioInterrupt { device, .. } => Some(device.as_str()),
                Resource::I2c { .. } => return None,
            },
        };
        Some(ImuBinding {
            model: ImuModel::Bmi270,
            path,
            address: device.address,
            frequency: *frequency,
            irq_path,
            stack_bytes: self.stack_bytes,
        })
    }

    pub fn mode(&self) -> Mode {
        self.mode
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
enum Resource {
    I2c { device: String, frequency: u32 },
    GpioInterrupt { device: String, trigger: Trigger },
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum Trigger {
    Rising,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Device {
    compatible: String,
    bus: String,
    address: u8,
    wake: Wake,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
enum Wake {
    Periodic {},
    Interrupt { resource: String },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Header {
    version: u32,
    mode: Mode,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Catalog {
    version: u32,
    binding: Vec<CatalogBinding>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogBinding {
    compatible: String,
    bus: String,
    model: String,
    addresses: Vec<u8>,
    frequencies: Vec<u32>,
}

/// Resolve the IMU part of a profile. `imu_provider` is its selected HAL
/// implementation (`nuttx`, `mock`, or `none`).
pub fn resolve(data: &Value, imu_provider: &str) -> Result<Option<Composition>> {
    let has_graph_fields = ["resources", "devices", "roles"]
        .iter()
        .any(|key| data.get(*key).is_some());
    let legacy = data.get("imu");
    let Some(header_value) = data.get("composition") else {
        if has_graph_fields {
            return Err("resources/devices/roles require a composition version header".into());
        }
        let Some(legacy) = legacy else {
            if imu_provider == "nuttx" {
                return Err("nuttx IMU requires device configuration".into());
            }
            return Ok(None);
        };
        let catalog = catalog()?;
        return resolve_legacy(legacy, imu_provider, &catalog).map(Some);
    };

    let catalog = catalog()?;
    let header: Header = decode(header_value, "invalid composition header")?;
    if header.version != 1 {
        return Err(format!(
            "unsupported composition version: {}",
            header.version
        ));
    }
    if legacy.is_some_and(|imu| !keys(imu, &[], &["stack-bytes"])) {
        return Err(
            "v1 composition permits only imu.stack-bytes; legacy IMU fields cannot be mixed".into(),
        );
    }
    if let Some(bindings) = data["hal"].as_object() {
        match header.mode {
            Mode::Hardware if bindings.values().any(|provider| provider == "mock") => {
                return Err("hardware composition cannot select mock HAL bindings".into());
            }
            Mode::Simulation
                if bindings
                    .values()
                    .any(|provider| provider != "mock" && provider != "none") =>
            {
                return Err("simulation composition cannot select physical HAL bindings".into());
            }
            _ => {}
        }
    }
    let stack_bytes = crate::profile::sensor_stack(legacy.unwrap_or(&Value::Null))?;
    let resources = decode_map(
        data.get("resources"),
        "composition requires top-level [resources]",
    )?;
    let devices: BTreeMap<String, Device> = decode_map(
        data.get("devices"),
        "composition requires top-level [devices]",
    )?;
    let roles: BTreeMap<String, String> =
        decode_map(data.get("roles"), "composition requires top-level [roles]")?;
    validate_map_sizes(&resources, &devices, &roles)?;
    let resources = normalize_resources(resources)?;
    validate_ids(&resources, &devices, &roles)?;
    validate_roles(&roles)?;
    validate_provider_mode(imu_provider, header.mode, &devices, &roles)?;
    validate_graph(&catalog, &resources, &devices, &roles)?;
    let graph = Composition {
        version: header.version,
        mode: header.mode,
        resources,
        devices,
        roles,
        stack_bytes,
    };
    Ok(Some(graph))
}

fn resolve_legacy(value: &Value, provider: &str, catalog: &Catalog) -> Result<Composition> {
    if !keys(
        value,
        &["driver", "device", "address", "frequency"],
        &["stack-bytes", "irq-device"],
    ) {
        return Err("invalid legacy IMU binding".into());
    }
    if value["driver"] != "bmi270" {
        return Err("unsupported legacy IMU model".into());
    }
    let address: u8 = number(&value["address"], "IMU address")?
        .try_into()
        .map_err(|_| "IMU address is out of range")?;
    let frequency = number(&value["frequency"], "IMU frequency")?
        .try_into()
        .map_err(|_| "IMU frequency is out of range")?;
    let bus_path = path(&value["device"])?;
    let irq = match value.get("irq-device") {
        None => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(v) => Some(path(v)?),
    };
    let mut resources = BTreeMap::new();
    resources.insert(
        "legacy_i2c".into(),
        Resource::I2c {
            device: bus_path,
            frequency,
        },
    );
    let wake = if let Some(irq_path) = irq {
        resources.insert(
            "legacy_irq".into(),
            Resource::GpioInterrupt {
                device: irq_path,
                trigger: Trigger::Rising,
            },
        );
        Wake::Interrupt {
            resource: "legacy_irq".into(),
        }
    } else {
        Wake::Periodic {}
    };
    let mut devices = BTreeMap::new();
    devices.insert(
        "imu".into(),
        Device {
            compatible: "bosch,bmi270".into(),
            bus: "legacy_i2c".into(),
            address,
            wake,
        },
    );
    let roles = BTreeMap::from([("imu".into(), "imu".into())]);
    let graph = Composition {
        version: 1,
        mode: Mode::Hardware,
        resources,
        devices,
        roles,
        stack_bytes: crate::profile::sensor_stack(value)?,
    };
    validate_provider_mode(provider, graph.mode, &graph.devices, &graph.roles)?;
    validate_graph(catalog, &graph.resources, &graph.devices, &graph.roles)?;
    Ok(graph)
}

fn validate_graph(
    catalog: &Catalog,
    resources: &BTreeMap<String, Resource>,
    devices: &BTreeMap<String, Device>,
    roles: &BTreeMap<String, String>,
) -> Result<()> {
    if catalog.version != 1 {
        return Err("unsupported IMU binding catalog version".into());
    }
    if devices.is_empty() {
        return Err("composition must define an IMU device".into());
    }
    let mut endpoints = BTreeMap::new();
    for (id, resource) in resources {
        let path =
            match resource {
                Resource::I2c { device, frequency } => {
                    if !catalog.binding.iter().any(|binding| {
                        binding.bus == "i2c" && binding.frequencies.contains(frequency)
                    }) {
                        return Err(format!(
                            "unsupported I2C frequency {frequency} in resource {id}"
                        ));
                    }
                    device
                }
                Resource::GpioInterrupt { device, .. } => device,
            };
        if let Some(previous) = endpoints.insert(path, resource) {
            if previous != resource {
                return Err(match (previous, resource) {
                    (Resource::I2c { .. }, Resource::I2c { .. }) => {
                        format!("conflicting I2C frequencies for {path}")
                    }
                    _ => format!("conflicting resource kinds for {path}"),
                });
            }
        }
    }

    let mut physical_claims = BTreeSet::new();
    let mut irq_claims = BTreeSet::new();
    for (id, device) in devices {
        let binding = binding_for(catalog, &device.compatible, "i2c")?;
        if binding.model != "bmi270" {
            return Err("binding catalog selects an unsupported IMU model".into());
        }
        if !binding.addresses.contains(&device.address) {
            return Err(format!(
                "unsupported {} address {:#04x}",
                device.compatible, device.address
            ));
        }
        let bus = resources
            .get(&device.bus)
            .ok_or_else(|| format!("device {id} references unknown bus {}", device.bus))?;
        let Resource::I2c {
            device: path,
            frequency,
        } = bus
        else {
            return Err(format!("device {id} requires an I2C bus"));
        };
        if !binding.frequencies.contains(frequency) {
            return Err(format!(
                "unsupported {} I2C frequency {frequency}",
                device.compatible
            ));
        }
        if !physical_claims.insert((path.as_str(), device.address)) {
            return Err("conflicting physical device address claim".into());
        }
        if let Wake::Interrupt { resource } = &device.wake {
            match resources
                .get(resource)
                .ok_or_else(|| format!("device {id} references unknown IRQ resource {resource}"))?
            {
                Resource::GpioInterrupt {
                    device: irq,
                    trigger: Trigger::Rising,
                } => {
                    if !irq_claims.insert(irq.as_str()) {
                        return Err("conflicting exclusive IRQ resource claim".into());
                    }
                }
                Resource::I2c { .. } => {
                    return Err(format!("device {id} IRQ reference is not a GPIO interrupt"))
                }
            }
        }
    }
    let role_device = roles
        .get("imu")
        .ok_or("composition must assign the default roles.imu device")?;
    if !devices.contains_key(role_device) {
        return Err("roles.imu references an unknown device".into());
    }
    Ok(())
}

fn validate_provider_mode(
    provider: &str,
    mode: Mode,
    devices: &BTreeMap<String, Device>,
    roles: &BTreeMap<String, String>,
) -> Result<()> {
    if mode == Mode::Simulation {
        if provider == "nuttx" || !devices.is_empty() || !roles.is_empty() {
            return Err("simulation composition cannot select a physical IMU provider or claim physical devices".into());
        }
        if provider == "mock" {
            return Err("simulation composition has no implemented mock IMU source".into());
        }
        return Err("simulation composition has no implemented IMU source".into());
    }
    if provider != "nuttx" {
        return Err(format!(
            "{mode:?} IMU composition requires the nuttx provider"
        ));
    }
    Ok(())
}

fn binding_for<'a>(
    catalog: &'a Catalog,
    compatible: &str,
    bus: &str,
) -> Result<&'a CatalogBinding> {
    let mut found = None;
    for binding in &catalog.binding {
        if binding.compatible == compatible && binding.bus == bus {
            if found.is_some() {
                return Err(format!(
                    "ambiguous compatible/bus binding: {compatible}/{bus}"
                ));
            }
            found = Some(binding);
        }
    }
    found.ok_or_else(|| format!("unsupported compatible/bus binding: {compatible}/{bus}"))
}

fn catalog() -> Result<Catalog> {
    toml::from_str(CATALOG_SOURCE)
        .map_err(|e| format!("invalid checked-in IMU binding catalog: {e}"))
}
fn normalize_resources(
    mut resources: BTreeMap<String, Resource>,
) -> Result<BTreeMap<String, Resource>> {
    for resource in resources.values_mut() {
        match resource {
            Resource::I2c { device, .. } | Resource::GpioInterrupt { device, .. } => {
                *device = normalized_path(device)?
            }
        }
    }
    Ok(resources)
}
fn validate_map_sizes(
    resources: &BTreeMap<String, Resource>,
    devices: &BTreeMap<String, Device>,
    roles: &BTreeMap<String, String>,
) -> Result<()> {
    if resources.len() > 64 || devices.len() > 64 || roles.len() > 16 {
        return Err("composition graph exceeds bounded resource/device/role counts".into());
    }
    Ok(())
}
fn validate_ids(
    resources: &BTreeMap<String, Resource>,
    devices: &BTreeMap<String, Device>,
    roles: &BTreeMap<String, String>,
) -> Result<()> {
    for id in resources.keys().chain(devices.keys()).chain(roles.values()) {
        valid_id(id)?;
    }
    Ok(())
}
fn validate_roles(roles: &BTreeMap<String, String>) -> Result<()> {
    for role in roles.keys() {
        if role != "imu" {
            return Err(format!("unsupported capability role: {role}"));
        }
    }
    Ok(())
}
fn decode<T: DeserializeOwned>(value: &Value, message: &str) -> Result<T> {
    T::deserialize(value).map_err(|error| format!("{message}: {error}"))
}
fn decode_map<T: DeserializeOwned>(
    value: Option<&Value>,
    message: &str,
) -> Result<BTreeMap<String, T>> {
    let value = value.ok_or(message)?;
    decode(value, message)
}
fn keys(v: &Value, required: &[&str], optional: &[&str]) -> bool {
    v.as_object().is_some_and(|map| {
        required.iter().all(|key| map.contains_key(*key))
            && map
                .keys()
                .all(|key| required.contains(&key.as_str()) || optional.contains(&key.as_str()))
    })
}
fn number(v: &Value, what: &str) -> Result<u64> {
    v.as_u64()
        .ok_or_else(|| format!("{what} must be a nonnegative integer"))
}
fn path(v: &Value) -> Result<String> {
    let value = v.as_str().ok_or("device endpoint must be a string")?;
    normalized_path(value)
}
fn normalized_path(value: &str) -> Result<String> {
    let tail = value
        .strip_prefix("/dev/")
        .filter(|tail| {
            tail.len() <= 120
                && tail
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"/_.-".contains(&byte))
                && tail.split('/').all(|part| part != "..")
        })
        .ok_or_else(|| format!("invalid /dev resource path: {value}"))?;
    let mut normalized = String::from("/dev");
    for part in tail.split('/').filter(|part| !matches!(*part, "" | ".")) {
        normalized.push('/');
        normalized.push_str(part);
    }
    if normalized == "/dev" {
        return Err(format!("invalid /dev resource path: {value}"));
    }
    Ok(normalized)
}
fn valid_id(id: &str) -> Result<()> {
    if id.is_empty()
        || id.len() > 64
        || !id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    {
        Err(format!("invalid composition ID: {id}"))
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn profile(mode: &str, path: &str, address: u64, wake: Value) -> Value {
        json!({
            "composition": {"version":1, "mode":mode},
            "resources": {"i2c": {"kind":"i2c", "device":path, "frequency":400000}},
            "devices": {"motion": {"compatible":"bosch,bmi270", "bus":"i2c", "address":address, "wake":wake}},
            "roles": {"imu":"motion"},
            "imu": {"stack-bytes":16384}
        })
    }

    #[test]
    fn legacy_and_v1_resolve_to_the_same_imu_binding() {
        let legacy = json!({"imu":{"driver":"bmi270", "device":"/dev/i2c0", "address":104, "frequency":400000}});
        let versioned = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        let left = resolve(&legacy, "nuttx").unwrap().unwrap();
        let right = resolve(&versioned, "nuttx").unwrap().unwrap();
        assert_eq!(left.imu(), right.imu());
        assert_eq!(right.imu().unwrap().irq_path, None);
    }

    #[test]
    fn qualification_mode_and_irq_resolve_to_typed_values() {
        let mut value = profile(
            "qualification",
            "/dev/i2c0",
            105,
            json!({"kind":"interrupt", "resource":"ready"}),
        );
        value["resources"]["ready"] =
            json!({"kind":"gpio-interrupt", "device":"/dev/gpio0", "trigger":"rising"});
        let composition = resolve(&value, "nuttx").unwrap().unwrap();
        assert_eq!(composition.mode(), Mode::Qualification);
        assert_eq!(composition.imu().unwrap().irq_path, Some("/dev/gpio0"));
        assert_eq!(composition.imu().unwrap().model, ImuModel::Bmi270);
    }

    #[test]
    fn rejects_mixed_legacy_fields_unknown_keys_and_orphan_graphs() {
        let mut mixed = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        mixed["imu"]["driver"] = json!("bmi270");
        assert!(resolve(&mixed, "nuttx").is_err());
        let mut extra = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        extra["devices"]["motion"]["extra"] = json!(true);
        assert!(resolve(&extra, "nuttx").is_err());
        let mut stale_wake = profile(
            "hardware",
            "/dev/i2c0",
            104,
            json!({"kind":"periodic", "resource":"irq"}),
        );
        assert!(resolve(&stale_wake, "nuttx").is_err());
        stale_wake["composition"]["version"] = json!(2);
        assert!(resolve(&stale_wake, "nuttx")
            .unwrap_err()
            .contains("version"));
        assert!(resolve(&json!({"resources":{}}), "none")
            .unwrap_err()
            .contains("version header"));
        let nested_legacy_shape = json!({
            "composition": {
                "version": 1,
                "mode": "hardware",
                "resources": {"i2c": {"kind":"i2c", "device":"/dev/i2c0", "frequency":400000}},
                "devices": {"motion": {"compatible":"bosch,bmi270", "bus":"i2c", "address":104, "wake":{"kind":"periodic"}}},
                "roles": {"imu":"motion"}
            },
            "imu": {"stack-bytes":16384}
        });
        assert!(resolve(&nested_legacy_shape, "nuttx").is_err());
    }

    #[test]
    fn normalizes_paths_for_alias_conflict_detection_and_serialization() {
        let mut conflict = profile("hardware", "/dev//i2c0", 104, json!({"kind":"periodic"}));
        conflict["devices"]["other"] = json!({"compatible":"bosch,bmi270", "bus":"i2c", "address":104, "wake":{"kind":"periodic"}});
        assert!(resolve(&conflict, "nuttx")
            .unwrap_err()
            .contains("address claim"));
        let valid = profile("hardware", "/dev//i2c0", 105, json!({"kind":"periodic"}));
        assert_eq!(
            resolve(&valid, "nuttx")
                .unwrap()
                .unwrap()
                .imu()
                .unwrap()
                .path,
            "/dev/i2c0"
        );
    }

    #[test]
    fn allows_distinct_addresses_and_rejects_speed_irq_and_simulation_conflicts() {
        let mut distinct = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        distinct["devices"]["other"] = json!({"compatible":"bosch,bmi270", "bus":"i2c", "address":105, "wake":{"kind":"periodic"}});
        assert!(resolve(&distinct, "nuttx").is_ok());

        let mut speed = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        speed["resources"]["alias"] =
            json!({"kind":"i2c", "device":"/dev//i2c0", "frequency":100000});
        assert!(resolve(&speed, "nuttx")
            .unwrap_err()
            .contains("frequencies"));

        let mut bad_irq = profile(
            "hardware",
            "/dev/i2c0",
            104,
            json!({"kind":"interrupt", "resource":"irq"}),
        );
        bad_irq["resources"]["irq"] =
            json!({"kind":"gpio-interrupt", "device":"/dev/gpio0", "trigger":"falling"});
        assert!(resolve(&bad_irq, "nuttx").is_err());

        let mut aliased_irq = profile(
            "hardware",
            "/dev/i2c0",
            104,
            json!({"kind":"interrupt", "resource":"irq"}),
        );
        aliased_irq["resources"]["irq"] =
            json!({"kind":"gpio-interrupt", "device":"/dev//gpio0", "trigger":"rising"});
        aliased_irq["resources"]["irq_alias"] =
            json!({"kind":"gpio-interrupt", "device":"/dev/gpio0", "trigger":"rising"});
        aliased_irq["devices"]["other"] = json!({
            "compatible":"bosch,bmi270", "bus":"i2c", "address":105,
            "wake":{"kind":"interrupt", "resource":"irq_alias"}
        });
        assert!(resolve(&aliased_irq, "nuttx")
            .unwrap_err()
            .contains("exclusive IRQ"));

        let physical = profile("simulation", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        assert!(resolve(&physical, "nuttx")
            .unwrap_err()
            .contains("physical IMU"));

        let mut mixed = distinct;
        mixed["hal"] = json!({"imu":"nuttx","gnss":"mock"});
        assert!(resolve(&mixed, "nuttx").unwrap_err().contains("mock HAL"));
    }

    #[test]
    fn validates_unselected_devices_and_stable_serialization() {
        let mut invalid = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        invalid["devices"]["other"] = json!({"compatible":"bosch,bmi270", "bus":"i2c", "address":200, "wake":{"kind":"periodic"}});
        assert!(resolve(&invalid, "nuttx").unwrap_err().contains("address"));
        let a = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        let b = json!({"imu":{"stack-bytes":16384},"roles":{"imu":"motion"},"devices":{"motion":{"wake":{"kind":"periodic"},"address":104,"bus":"i2c","compatible":"bosch,bmi270"}},"resources":{"i2c":{"frequency":400000,"device":"/dev/i2c0","kind":"i2c"}},"composition":{"mode":"hardware","version":1}});
        let a = serde_json::to_vec(&resolve(&a, "nuttx").unwrap().unwrap()).unwrap();
        let b = serde_json::to_vec(&resolve(&b, "nuttx").unwrap().unwrap()).unwrap();
        assert_eq!(a, b);
    }
}
