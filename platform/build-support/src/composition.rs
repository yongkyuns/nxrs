//! Typed, build-time validation for versioned peripheral composition profiles.
use crate::Result;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::PathBuf;

/// Checked-in capability-owned binding catalog embedded in the resolver.
pub const BINDING_SCHEMA_PATH: &str = "hal/imu/bindings/i2c.toml";
const CATALOG_SOURCE: &str = include_str!("../../../hal/imu/bindings/i2c.toml");

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Hardware,
    Simulation,
    Qualification,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ImuModel {
    Bmi270,
    Lsm6dso,
}

/// Normalized graph. Maps serialize in key order and endpoint paths are normalized.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Composition {
    version: u32,
    mode: Mode,
    resources: BTreeMap<String, Resource>,
    devices: BTreeMap<String, Device>,
    roles: BTreeMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    imu_model: Option<ImuModel>,
    stack_bytes: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    simulation: Option<SimulationSpec>,
}

/// Seed and enabled endpoint identities for one coordinated simulation run.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SimulationSpec {
    pub seed: u64,
    pub endpoints: std::collections::BTreeSet<SimulationEndpoint>,
}

/// Stable identity of a supported simulated capability endpoint.
#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "lowercase")]
pub enum SimulationEndpoint {
    Imu,
    Gnss,
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
            model: self.imu_model?,
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

    pub fn simulation(&self) -> Option<&SimulationSpec> {
        self.simulation.as_ref()
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
struct SimulationInput {
    seed: u64,
    endpoints: Vec<String>,
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
    model: ImuModel,
    addresses: Vec<u8>,
    frequencies: Vec<u32>,
    interrupt: bool,
}

/// Resolve the IMU part of a profile. `imu_provider` is its selected HAL
/// implementation (`nuttx`, `mock`, or `none`).
pub fn resolve(data: &Value, imu_provider: &str) -> Result<Option<Composition>> {
    resolve_with_origins(data, imu_provider, &BTreeMap::new())
}

/// Resolve with source locations for profile fields. Keys use dotted paths and
/// may include tombstones for removed records, allowing dangling references to
/// point back to the overlay that removed their target.
pub(crate) fn resolve_with_origins(
    data: &Value,
    imu_provider: &str,
    origins: &BTreeMap<String, PathBuf>,
) -> Result<Option<Composition>> {
    let has_graph_fields = ["resources", "devices", "roles"]
        .iter()
        .any(|key| data.get(*key).is_some());
    let legacy = data.get("imu");
    let Some(header_value) = data.get("composition") else {
        if data
            .pointer("/hal")
            .and_then(Value::as_object)
            .is_some_and(|bindings| bindings.values().any(|provider| provider == "simulation"))
        {
            return Err(diagnostic(
                origins,
                "composition",
                "simulation HAL bindings require composition.mode = simulation",
            ));
        }
        if has_graph_fields {
            return Err(diagnostic(
                origins,
                "resources",
                "resources/devices/roles require a composition version header",
            ));
        }
        let Some(legacy) = legacy else {
            if imu_provider == "nuttx" {
                return Err(diagnostic(
                    origins,
                    "imu",
                    "nuttx IMU requires device configuration",
                ));
            }
            return Ok(None);
        };
        let catalog = catalog()?;
        return resolve_legacy(legacy, imu_provider, &catalog, origins).map(Some);
    };

    let catalog = catalog()?;
    let header: Header = decode(header_value, "invalid composition header")
        .map_err(|error| diagnostic(origins, "composition", &error))?;
    if header.version != 1 {
        return Err(diagnostic(
            origins,
            "composition.version",
            &format!("unsupported composition version: {}", header.version),
        ));
    }
    if header.mode != Mode::Simulation
        && data
            .pointer("/hal")
            .and_then(Value::as_object)
            .is_some_and(|bindings| bindings.values().any(|provider| provider == "simulation"))
    {
        return Err(diagnostic(
            origins,
            "hal",
            "simulation HAL bindings require composition.mode = simulation",
        ));
    }
    let simulation = match (header.mode, data.get("simulation")) {
        (Mode::Simulation, Some(value)) => {
            let raw: SimulationInput = decode(value, "invalid simulation configuration")
                .map_err(|error| diagnostic(origins, "simulation", &error))?;
            let endpoint_count = raw.endpoints.len();
            let endpoints: std::collections::BTreeSet<_> = raw
                .endpoints
                .into_iter()
                .map(|endpoint| match endpoint.as_str() {
                    "imu" => Ok(SimulationEndpoint::Imu),
                    "gnss" => Ok(SimulationEndpoint::Gnss),
                    _ => Err(endpoint),
                })
                .collect::<std::result::Result<_, _>>()
                .map_err(|endpoint| {
                    diagnostic(
                        origins,
                        "simulation.endpoints",
                        &format!("unsupported simulation endpoint: {endpoint}"),
                    )
                })?;
            if endpoints.is_empty() {
                return Err(diagnostic(
                    origins,
                    "simulation.endpoints",
                    "simulation requires at least one endpoint",
                ));
            }
            if endpoint_count != endpoints.len() {
                return Err(diagnostic(
                    origins,
                    "simulation.endpoints",
                    "simulation endpoint identities must be unique",
                ));
            }
            Some(SimulationSpec {
                seed: raw.seed,
                endpoints,
            })
        }
        (Mode::Simulation, None) => {
            if ["resources", "devices", "roles", "imu", "gnss"]
                .iter()
                .any(|key| data.get(*key).is_some())
            {
                return Err(diagnostic(
                    origins,
                    "composition.mode",
                    "simulation composition cannot select a physical IMU provider or claim physical devices",
                ));
            }
            return Err(diagnostic(
                origins,
                "simulation",
                "simulation mode requires [simulation]",
            ));
        }
        (_, Some(_)) => {
            return Err(diagnostic(
                origins,
                "simulation",
                "[simulation] is valid only in simulation mode",
            ));
        }
        (_, None) => None,
    };
    if let Some(spec) = &simulation {
        if ["resources", "devices", "roles", "imu", "gnss"]
            .iter()
            .any(|key| data.get(*key).is_some())
        {
            return Err(diagnostic(
                origins,
                "composition.mode",
                "simulation profiles cannot declare physical resources, devices, or roles",
            ));
        }
        let bindings = data["hal"].as_object().ok_or("[hal] must be a table")?;
        let expected: std::collections::BTreeSet<&str> = spec
            .endpoints
            .iter()
            .map(|endpoint| match endpoint {
                SimulationEndpoint::Imu => "imu",
                SimulationEndpoint::Gnss => "gnss",
            })
            .collect();
        let actual: std::collections::BTreeSet<&str> = bindings
            .iter()
            .filter_map(|(capability, provider)| {
                (provider == "simulation").then_some(capability.as_str())
            })
            .collect();
        if actual != expected
            || expected.iter().any(|capability| {
                bindings.get(*capability).and_then(Value::as_str) != Some("simulation")
            })
            || bindings.iter().any(|(capability, provider)| {
                provider.as_str() != Some("none") && !expected.contains(capability.as_str())
            })
        {
            return Err(diagnostic(
                origins,
                "hal",
                "simulation endpoint set must exactly match simulation HAL bindings; other active HALs are unsupported",
            ));
        }
    }
    if legacy.is_some_and(|imu| !keys(imu, &[], &["stack-bytes"])) {
        return Err(diagnostic(
            origins,
            "imu",
            "v1 composition permits only imu.stack-bytes; legacy IMU fields cannot be mixed",
        ));
    }
    if let Some(bindings) = data["hal"].as_object() {
        match header.mode {
            Mode::Hardware if bindings.values().any(|provider| provider == "mock") => {
                return Err(diagnostic(
                    origins,
                    "hal",
                    "hardware composition cannot select mock HAL bindings",
                ));
            }
            Mode::Simulation
                if bindings.iter().any(|(capability, provider)| {
                    provider != "none"
                        && !(provider == "simulation"
                            && ["imu", "gnss"].contains(&capability.as_str()))
                }) =>
            {
                return Err(diagnostic(
                    origins,
                    "hal",
                    "simulation composition supports only simulation IMU/GNSS bindings",
                ));
            }
            _ => {}
        }
    }
    let stack_bytes = crate::profile::sensor_stack(legacy.unwrap_or(&Value::Null))
        .map_err(|error| diagnostic(origins, "imu.stack-bytes", &error))?;
    if header.mode == Mode::Simulation {
        return Ok(Some(Composition {
            version: header.version,
            mode: header.mode,
            resources: BTreeMap::new(),
            devices: BTreeMap::new(),
            roles: BTreeMap::new(),
            imu_model: None,
            stack_bytes,
            simulation,
        }));
    }
    let resources = decode_map(
        data.get("resources"),
        "resources",
        "composition requires top-level [resources]",
        origins,
    )?;
    let devices: BTreeMap<String, Device> = decode_map(
        data.get("devices"),
        "devices",
        "composition requires top-level [devices]",
        origins,
    )?;
    let roles: BTreeMap<String, String> = decode_map(
        data.get("roles"),
        "roles",
        "composition requires top-level [roles]",
        origins,
    )?;
    validate_map_sizes(&resources, &devices, &roles)
        .map_err(|e| diagnostic(origins, "resources", &e))?;
    let resources = normalize_resources(resources, origins)?;
    validate_ids(&resources, &devices, &roles, origins)?;
    validate_roles(&roles).map_err(|e| diagnostic(origins, "roles", &e))?;
    validate_provider_mode(imu_provider, header.mode, &devices, &roles)
        .map_err(|e| diagnostic(origins, "composition.mode", &e))?;
    let imu_model = validate_graph(&catalog, &resources, &devices, &roles, origins)?;
    let graph = Composition {
        version: header.version,
        mode: header.mode,
        resources,
        devices,
        roles,
        imu_model: Some(imu_model),
        stack_bytes,
        simulation,
    };
    Ok(Some(graph))
}

fn resolve_legacy(
    value: &Value,
    provider: &str,
    catalog: &Catalog,
    origins: &BTreeMap<String, PathBuf>,
) -> Result<Composition> {
    if !keys(
        value,
        &["driver", "device", "address", "frequency"],
        &["stack-bytes", "irq-device"],
    ) {
        return Err(diagnostic(origins, "imu", "invalid legacy IMU binding"));
    }
    if value["driver"] != "bmi270" {
        return Err(diagnostic(
            origins,
            "imu.driver",
            "unsupported legacy IMU model",
        ));
    }
    let address: u8 = number(&value["address"], "IMU address")
        .map_err(|e| diagnostic(origins, "imu.address", &e))?
        .try_into()
        .map_err(|_| diagnostic(origins, "imu.address", "IMU address is out of range"))?;
    let frequency = number(&value["frequency"], "IMU frequency")
        .map_err(|e| diagnostic(origins, "imu.frequency", &e))?
        .try_into()
        .map_err(|_| diagnostic(origins, "imu.frequency", "IMU frequency is out of range"))?;
    let bus_path = path(&value["device"]).map_err(|e| diagnostic(origins, "imu.device", &e))?;
    let irq = match value.get("irq-device") {
        None => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(v) => Some(path(v).map_err(|e| diagnostic(origins, "imu.irq-device", &e))?),
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
    validate_provider_mode(provider, Mode::Hardware, &devices, &roles)?;
    let imu_model = validate_graph(catalog, &resources, &devices, &roles, origins)?;
    let graph = Composition {
        version: 1,
        mode: Mode::Hardware,
        resources,
        devices,
        roles,
        imu_model: Some(imu_model),
        stack_bytes: crate::profile::sensor_stack(value)?,
        simulation: None,
    };
    Ok(graph)
}

fn validate_graph(
    catalog: &Catalog,
    resources: &BTreeMap<String, Resource>,
    devices: &BTreeMap<String, Device>,
    roles: &BTreeMap<String, String>,
    origins: &BTreeMap<String, PathBuf>,
) -> Result<ImuModel> {
    if catalog.version != 1 {
        return Err(diagnostic(
            origins,
            "binding.catalog.version",
            "unsupported IMU binding catalog version",
        ));
    }
    let mut endpoints: BTreeMap<&str, (&str, &Resource)> = BTreeMap::new();
    for (id, resource) in resources {
        let path =
            match resource {
                Resource::I2c { device, frequency } => {
                    if !catalog.binding.iter().any(|binding| {
                        binding.bus == "i2c" && binding.frequencies.contains(frequency)
                    }) {
                        return Err(diagnostic(
                            origins,
                            &format!("resources.{id}.frequency"),
                            &format!("unsupported I2C frequency {frequency}"),
                        ));
                    }
                    device
                }
                Resource::GpioInterrupt { device, .. } => device,
            };
        if let Some((previous_id, previous)) = endpoints.insert(path, (id, resource)) {
            if previous != resource {
                let (related, message) = match (previous, resource) {
                    (Resource::I2c { .. }, Resource::I2c { .. }) => (
                        format!("resources.{previous_id}.frequency"),
                        format!("conflicting I2C frequencies for endpoint {path}"),
                    ),
                    _ => (
                        format!("resources.{previous_id}.kind"),
                        format!("conflicting resource kinds for endpoint {path}"),
                    ),
                };
                let current = if matches!(resource, Resource::I2c { .. }) {
                    format!("resources.{id}.frequency")
                } else {
                    format!("resources.{id}.kind")
                };
                return Err(diagnostic_related(origins, &current, &[&related], &message));
            }
        }
    }

    let mut physical_claims = BTreeMap::new();
    let mut irq_claims = BTreeMap::new();
    for (id, device) in devices {
        let binding = binding_for(catalog, &device.compatible, "i2c")
            .map_err(|error| diagnostic(origins, &format!("devices.{id}.compatible"), &error))?;
        if !binding.addresses.contains(&device.address) {
            return Err(diagnostic(
                origins,
                &format!("devices.{id}.address"),
                &format!(
                    "unsupported {} address {:#04x}",
                    device.compatible, device.address
                ),
            ));
        }
        let bus = resources.get(&device.bus).ok_or_else(|| {
            diagnostic_related(
                origins,
                &format!("devices.{id}.bus"),
                &[&format!("resources.{}", device.bus)],
                &format!("references unknown bus {}", device.bus),
            )
        })?;
        let Resource::I2c {
            device: path,
            frequency,
        } = bus
        else {
            return Err(diagnostic_related(
                origins,
                &format!("devices.{id}.bus"),
                &[&format!("resources.{}.kind", device.bus)],
                "requires an I2C bus",
            ));
        };
        if !binding.frequencies.contains(frequency) {
            return Err(diagnostic(
                origins,
                &format!("resources.{}.frequency", device.bus),
                &format!(
                    "unsupported {} I2C frequency {frequency}",
                    device.compatible
                ),
            ));
        }
        if let Some((previous_id, previous_bus)) =
            physical_claims.insert((path.as_str(), device.address), (id, &device.bus))
        {
            return Err(diagnostic_related(
                origins,
                &format!("devices.{id}.address"),
                &[
                    &format!("devices.{previous_id}.address"),
                    &format!("devices.{id}.bus"),
                    &format!("devices.{previous_id}.bus"),
                    &format!("resources.{}.device", device.bus),
                    &format!("resources.{previous_bus}.device"),
                ],
                "conflicting physical device address claim",
            ));
        }
        if let Wake::Interrupt { resource } = &device.wake {
            if !binding.interrupt {
                return Err(diagnostic(
                    origins,
                    &format!("devices.{id}.wake.kind"),
                    &format!("{} does not support interrupt wake", device.compatible),
                ));
            }
            match resources.get(resource).ok_or_else(|| {
                diagnostic_related(
                    origins,
                    &format!("devices.{id}.wake.resource"),
                    &[&format!("resources.{resource}")],
                    &format!("references unknown IRQ resource {resource}"),
                )
            })? {
                Resource::GpioInterrupt {
                    device: irq,
                    trigger: Trigger::Rising,
                } => {
                    if let Some(previous_id) = irq_claims.insert(irq.as_str(), id) {
                        return Err(diagnostic_related(
                            origins,
                            &format!("devices.{id}.wake.resource"),
                            &[&format!("devices.{previous_id}.wake.resource")],
                            "conflicting exclusive IRQ resource claim",
                        ));
                    }
                }
                Resource::I2c { .. } => {
                    return Err(diagnostic_related(
                        origins,
                        &format!("devices.{id}.wake.resource"),
                        &[&format!("resources.{resource}.kind")],
                        "IRQ reference is not a GPIO interrupt",
                    ))
                }
            }
        }
    }
    let role_device = roles.get("imu").ok_or_else(|| {
        diagnostic(
            origins,
            "roles.imu",
            "composition must assign the default IMU device",
        )
    })?;
    let device = devices.get(role_device).ok_or_else(|| {
        diagnostic_related(
            origins,
            "roles.imu",
            &[&format!("devices.{role_device}")],
            &format!("references unknown device {role_device}"),
        )
    })?;
    Ok(binding_for(catalog, &device.compatible, "i2c")?.model)
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

/// Digest of the exact checked-in schema embedded into this build-support crate.
pub(crate) fn binding_schema_digest() -> String {
    crate::hash_bytes(CATALOG_SOURCE.as_bytes())
}

fn diagnostic(origins: &BTreeMap<String, PathBuf>, field: &str, message: &str) -> String {
    let source = origin_for(origins, field);
    match source {
        Some(path) => format!("{field} (from {}): {message}", path.display()),
        None => format!("{field}: {message}"),
    }
}

fn diagnostic_related(
    origins: &BTreeMap<String, PathBuf>,
    field: &str,
    related: &[&str],
    message: &str,
) -> String {
    let mut result = diagnostic(origins, field, message);
    let primary_source = origin_for(origins, field);
    for related_field in related {
        if let Some(path) = origins.get(*related_field) {
            if primary_source != Some(path) {
                result.push_str(&format!("; {related_field} changed by {}", path.display()));
            }
        }
    }
    result
}

fn origin_for<'a>(origins: &'a BTreeMap<String, PathBuf>, field: &str) -> Option<&'a PathBuf> {
    let mut current = field;
    loop {
        if let Some(path) = origins.get(current) {
            return Some(path);
        }
        let Some((parent, _)) = current.rsplit_once('.') else {
            return origins.get("");
        };
        current = parent;
    }
}
fn normalize_resources(
    mut resources: BTreeMap<String, Resource>,
    origins: &BTreeMap<String, PathBuf>,
) -> Result<BTreeMap<String, Resource>> {
    for (id, resource) in &mut resources {
        match resource {
            Resource::I2c { device, .. } | Resource::GpioInterrupt { device, .. } => {
                *device = normalized_path(device).map_err(|error| {
                    diagnostic(origins, &format!("resources.{id}.device"), &error)
                })?
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
    origins: &BTreeMap<String, PathBuf>,
) -> Result<()> {
    for id in resources.keys() {
        valid_id(id).map_err(|error| diagnostic(origins, &format!("resources.{id}"), &error))?;
    }
    for id in devices.keys() {
        valid_id(id).map_err(|error| diagnostic(origins, &format!("devices.{id}"), &error))?;
    }
    for (role, id) in roles {
        valid_id(id).map_err(|error| diagnostic(origins, &format!("roles.{role}"), &error))?;
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
    field: &str,
    message: &str,
    origins: &BTreeMap<String, PathBuf>,
) -> Result<BTreeMap<String, T>> {
    let value = value.ok_or_else(|| diagnostic(origins, field, message))?;
    let Some(values) = value.as_object() else {
        return decode(value, message).map_err(|error| diagnostic(origins, field, &error));
    };
    let mut decoded = BTreeMap::new();
    for (id, value) in values {
        let item = T::deserialize(value)
            .map_err(|error| diagnostic(origins, &format!("{field}.{id}"), &error.to_string()))?;
        decoded.insert(id.clone(), item);
    }
    Ok(decoded)
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
    fn replacement_lsm6dso_requires_its_own_address_and_periodic_wakes() {
        let mut replacement = profile("hardware", "/dev/i2c1", 106, json!({"kind":"periodic"}));
        replacement["devices"]["motion"]["compatible"] = json!("st,lsm6dso");
        let resolved = resolve(&replacement, "nuttx").unwrap().unwrap();
        let binding = resolved.imu().unwrap();
        assert_eq!(binding.model, ImuModel::Lsm6dso);
        assert_eq!((binding.path, binding.address), ("/dev/i2c1", 106));

        replacement["devices"]["motion"]["address"] = json!(104);
        assert!(resolve(&replacement, "nuttx")
            .unwrap_err()
            .contains("address"));
        replacement["devices"]["motion"]["address"] = json!(106);
        replacement["devices"]["motion"]["wake"] = json!({"kind":"interrupt","resource":"irq"});
        assert!(resolve(&replacement, "nuttx")
            .unwrap_err()
            .contains("does not support interrupt wake"));
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

    #[test]
    fn diagnostics_follow_field_origins_and_removed_reference_targets() {
        let value = profile(
            "hardware",
            "/dev/i2c0",
            104,
            json!({"kind":"interrupt", "resource":"ready"}),
        );
        let origins = BTreeMap::from([
            (
                "devices.motion.wake.resource".into(),
                PathBuf::from("base.toml"),
            ),
            (
                "resources.ready".into(),
                PathBuf::from("overlays/remove-ready.toml"),
            ),
        ]);
        let error = resolve_with_origins(&value, "nuttx", &origins).unwrap_err();
        assert!(error.contains("devices.motion.wake.resource"));
        assert!(error.contains("resources.ready changed by overlays/remove-ready.toml"));
    }

    #[test]
    fn binding_schema_digest_matches_embedded_catalog_bytes() {
        assert_eq!(
            binding_schema_digest(),
            crate::hash_bytes(CATALOG_SOURCE.as_bytes())
        );
    }

    fn simulation_profile() -> Value {
        json!({
            "composition":{"version":1,"mode":"simulation"},
            "simulation":{"seed":274,"endpoints":["imu","gnss"]},
            "hal":{"imu":"simulation","gnss":"simulation"}
        })
    }

    #[test]
    fn resolves_coordinated_simulation_endpoints_and_seed() {
        let composition = resolve(&simulation_profile(), "simulation")
            .unwrap()
            .unwrap();
        assert_eq!(composition.mode(), Mode::Simulation);
        let spec = composition.simulation().unwrap();
        assert_eq!(spec.seed, 274);
        assert_eq!(
            spec.endpoints,
            std::collections::BTreeSet::from([SimulationEndpoint::Imu, SimulationEndpoint::Gnss])
        );
        assert_eq!(composition.imu(), None);
        let encoded = serde_json::to_value(&composition).unwrap();
        assert_eq!(encoded["simulation"]["seed"], 274);
        assert!(encoded.get("imu_model").is_none());
        assert_eq!(encoded["simulation"]["endpoints"], json!(["imu", "gnss"]));
    }

    #[test]
    fn simulation_rejects_ambiguous_or_physical_composition() {
        let mut value = simulation_profile();
        value["simulation"]["endpoints"] = json!([]);
        assert!(resolve(&value, "simulation")
            .unwrap_err()
            .contains("at least one"));

        let mut value = simulation_profile();
        value["simulation"]["endpoints"] = json!(["imu", "imu"]);
        assert!(resolve(&value, "simulation")
            .unwrap_err()
            .contains("unique"));

        let mut value = simulation_profile();
        value["simulation"]["endpoints"] = json!(["camera"]);
        assert!(resolve(&value, "simulation")
            .unwrap_err()
            .contains("unsupported simulation endpoint"));

        for key in ["resources", "devices", "roles", "imu", "gnss"] {
            let mut value = simulation_profile();
            value[key] = json!({});
            assert!(
                resolve(&value, "simulation")
                    .unwrap_err()
                    .contains("cannot declare physical"),
                "{key}"
            );
        }

        let mut value = simulation_profile();
        value["hal"]["imu"] = json!("nuttx");
        let error = resolve(&value, "simulation").unwrap_err();
        assert!(error.contains("simulation"), "{error}");

        let mut value = simulation_profile();
        value["hal"]["camera"] = json!("mock");
        assert!(resolve(&value, "simulation")
            .unwrap_err()
            .contains("exactly match"));
    }

    #[test]
    fn simulation_table_is_exclusive_to_simulation_mode() {
        let mut value = simulation_profile();
        value["composition"]["mode"] = json!("hardware");
        value["hal"] = json!({"imu":"none", "gnss":"none"});
        assert!(resolve(&value, "simulation")
            .unwrap_err()
            .contains("valid only in simulation mode"));

        let mut value = simulation_profile();
        value.as_object_mut().unwrap().remove("simulation");
        assert!(resolve(&value, "simulation")
            .unwrap_err()
            .contains("requires [simulation]"));

        let value = json!({"hal":{"imu":"simulation"}});
        assert!(resolve(&value, "simulation")
            .unwrap_err()
            .contains("require composition.mode = simulation"));
    }
}
