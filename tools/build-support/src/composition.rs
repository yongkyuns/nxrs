//! Typed, build-time validation for versioned peripheral composition profiles.
use crate::{bindings::Catalogs, Result};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Hardware,
    Simulation,
    Qualification,
}

/// Normalized graph. Maps serialize in key order and endpoint paths are normalized.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Composition {
    version: u32,
    mode: Mode,
    pub(crate) resources: BTreeMap<String, Resource>,
    pub(crate) devices: BTreeMap<String, Device>,
    pub(crate) aliases: BTreeMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    imu_implementation: Option<String>,
    stack_bytes: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    gnss_implementation: Option<String>,
    gnss_stack_bytes: u64,
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImuTransport {
    I2c { address: u8 },
    Spi { chip_select: u16, mode: u8 },
}

/// Borrowed view of the resolved default IMU device.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImuBinding<'a> {
    pub implementation: &'a str,
    pub path: &'a str,
    pub transport: ImuTransport,
    pub frequency: u32,
    pub irq_path: Option<&'a str>,
    pub stack_bytes: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GnssBinding<'a> {
    pub implementation: &'a str,
    pub path: &'a str,
    pub baud: u32,
    pub stack_bytes: u64,
}

impl Composition {
    pub(crate) fn devices_for<'a>(
        &'a self,
        catalogs: &'a Catalogs,
        capability: &'a str,
    ) -> impl Iterator<Item = (&'a str, &'a Device)> {
        self.devices
            .iter()
            .filter(move |(_, device)| {
                catalogs
                    .values(capability)
                    .any(|binding| binding["compatible"] == device.compatible)
            })
            .map(|(id, device)| (id.as_str(), device))
    }

    /// Direct names and aliases share the same namespace and canonical objects.
    pub(crate) fn accessors<'a>(
        &'a self,
        catalogs: &'a Catalogs,
        capability: &'a str,
    ) -> Result<BTreeMap<String, &'a str>> {
        let mut names: BTreeMap<_, _> = self
            .devices_for(catalogs, capability)
            .map(|(id, _)| (id.to_owned(), id))
            .collect();
        for (alias, id) in &self.aliases {
            let Some(name) = alias_name(capability, alias) else {
                continue;
            };
            let name = name?;
            if !self
                .devices_for(catalogs, capability)
                .any(|(device, _)| device == id)
            {
                return Err(format!(
                    "{capability} alias {alias} must select a supported device"
                ));
            }
            if let Some(previous) = names.insert(name.clone(), id) {
                if previous != id {
                    return Err(format!(
                        "{capability} accessor {name} refers to both device {previous} and alias {alias} -> {id}"
                    ));
                }
            }
        }
        Ok(names)
    }

    pub fn imu(&self) -> Option<ImuBinding<'_>> {
        let device_id = self.aliases.get("imu")?;
        let device = self.devices.get(device_id)?;
        let (path, frequency, transport) = match self.resources.get(&device.bus)? {
            Resource::I2c {
                device: path,
                frequency,
            } => (
                path,
                *frequency,
                ImuTransport::I2c {
                    address: device.address?,
                },
            ),
            Resource::Spi {
                device: path,
                frequency,
            } => (
                path,
                *frequency,
                ImuTransport::Spi {
                    chip_select: device.chip_select?,
                    mode: device.spi_mode?,
                },
            ),
            _ => return None,
        };
        let irq_path = match device.wake.as_ref()? {
            Wake::Periodic {} => None,
            Wake::Interrupt { resource } => match self.resources.get(resource)? {
                Resource::GpioInterrupt { device, .. } => Some(device.as_str()),
                Resource::I2c { .. } | Resource::Spi { .. } | Resource::Uart { .. } => return None,
            },
        };
        Some(ImuBinding {
            implementation: self.imu_implementation.as_deref()?,
            path,
            transport,
            frequency,
            irq_path,
            stack_bytes: self.stack_bytes,
        })
    }

    pub fn gnss(&self) -> Option<GnssBinding<'_>> {
        let device_id = self.aliases.get("gnss")?;
        let device = self.devices.get(device_id)?;
        let Resource::Uart { device: path, baud } = self.resources.get(&device.bus)? else {
            return None;
        };
        Some(GnssBinding {
            implementation: self.gnss_implementation.as_deref()?,
            path,
            baud: *baud,
            stack_bytes: self.gnss_stack_bytes,
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
pub(crate) enum Resource {
    I2c { device: String, frequency: u32 },
    Spi { device: String, frequency: u32 },
    GpioInterrupt { device: String, trigger: Trigger },
    Uart { device: String, baud: u32 },
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Trigger {
    Rising,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Device {
    pub(crate) compatible: String,
    pub(crate) bus: String,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) address: Option<u8>,
    #[serde(
        default,
        rename = "chip-select",
        skip_serializing_if = "Option::is_none"
    )]
    pub(crate) chip_select: Option<u16>,
    #[serde(default, rename = "spi-mode", skip_serializing_if = "Option::is_none")]
    pub(crate) spi_mode: Option<u8>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) wake: Option<Wake>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
pub(crate) enum Wake {
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

// Temporary typed projections for the existing runtime ABI. D2 replaces these
// model enums and singular bindings with static device construction.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Catalog {
    binding: Vec<CatalogBinding>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogBinding {
    compatible: String,
    bus: String,
    implementation: String,
    #[serde(default)]
    addresses: Vec<u8>,
    #[serde(default)]
    modes: Vec<u8>,
    frequencies: Vec<u32>,
    interrupt: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GnssCatalog {
    binding: Vec<GnssCatalogBinding>,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GnssCatalogBinding {
    compatible: String,
    bus: String,
    implementation: String,
    bauds: Vec<u32>,
}

/// Resolve the IMU part of a profile. `imu_provider` is its selected HAL
/// implementation (`nuttx`, `mock`, or `none`).
pub fn resolve(
    data: &Value,
    imu_provider: &str,
    catalogs: &Catalogs,
) -> Result<Option<Composition>> {
    resolve_with_providers_and_origins(data, imu_provider, "none", &BTreeMap::new(), catalogs)
}

/// Resolve with source locations for profile fields. Keys use dotted paths and
/// may include tombstones for removed records, allowing dangling references to
/// point back to the overlay that removed their target.
#[cfg(test)]
pub(crate) fn resolve_with_origins(
    data: &Value,
    imu_provider: &str,
    origins: &BTreeMap<String, PathBuf>,
    catalogs: &Catalogs,
) -> Result<Option<Composition>> {
    resolve_with_providers_and_origins(data, imu_provider, "none", origins, catalogs)
}

pub(crate) fn resolve_with_providers_and_origins(
    data: &Value,
    imu_provider: &str,
    gnss_provider: &str,
    origins: &BTreeMap<String, PathBuf>,
    catalogs: &Catalogs,
) -> Result<Option<Composition>> {
    if data.get("roles").is_some() {
        return Err(diagnostic(origins, "roles", "rename [roles] to [aliases]"));
    }
    let has_graph_fields = ["resources", "devices", "aliases"]
        .iter()
        .any(|key| data.get(*key).is_some());
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
                "resources/devices/aliases require a composition version header",
            ));
        }
        if imu_provider == "nuttx"
            || gnss_provider == "nuttx"
            || ["imu", "gnss", "simulation"]
                .iter()
                .any(|field| data.get(*field).is_some())
        {
            return Err(diagnostic(
                origins,
                "composition",
                "sensor and simulation profiles require composition version 1 with explicit bindings",
            ));
        }
        return Ok(None);
    };

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
            if ["resources", "devices", "aliases", "imu", "gnss"]
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
        if ["resources", "devices", "aliases", "imu", "gnss"]
            .iter()
            .any(|key| data.get(*key).is_some())
        {
            return Err(diagnostic(
                origins,
                "composition.mode",
                "simulation profiles cannot declare physical resources, devices, or aliases",
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
    for sensor in ["imu", "gnss"] {
        if data.get(sensor).is_some_and(|settings| {
            !settings
                .as_object()
                .is_some_and(|fields| fields.keys().all(|key| key == "stack-bytes"))
        }) {
            return Err(diagnostic(
                origins,
                sensor,
                "composition version 1 uses resources, devices and aliases for physical bindings. Only stack-bytes belongs here",
            ));
        }
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
    let stack_bytes = crate::profile::sensor_stack(&data["imu"])
        .map_err(|error| diagnostic(origins, "imu.stack-bytes", &error))?;
    let gnss_stack_bytes = crate::profile::sensor_stack(&data["gnss"])
        .map_err(|error| diagnostic(origins, "gnss.stack-bytes", &error))?;
    if header.mode == Mode::Simulation {
        return Ok(Some(Composition {
            version: header.version,
            mode: header.mode,
            resources: BTreeMap::new(),
            devices: BTreeMap::new(),
            aliases: BTreeMap::new(),
            imu_implementation: None,
            stack_bytes,
            gnss_implementation: None,
            gnss_stack_bytes,
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
    let mut aliases: BTreeMap<String, String> = match data.get("aliases") {
        Some(value) => decode_map(Some(value), "aliases", "invalid [aliases]", origins)?,
        None => BTreeMap::new(),
    };
    let resources = normalize_resources(resources, origins)?;
    validate_ids(&resources, &devices, &aliases, origins)?;
    validate_aliases(&aliases).map_err(|e| diagnostic(origins, "aliases", &e))?;
    for (capability, provider) in [("imu", imu_provider), ("gnss", gnss_provider)] {
        if provider != "nuttx" {
            let prefix = format!("{capability}_");
            if let Some(alias) = aliases
                .keys()
                .find(|alias| alias.as_str() == capability || alias.starts_with(&prefix))
            {
                return Err(diagnostic(
                    origins,
                    &format!("aliases.{alias}"),
                    &format!("physical {capability} composition requires the nuttx provider"),
                ));
            }
        }
        if provider == "nuttx" && !aliases.contains_key(capability) {
            let mut candidates = devices.iter().filter(|(_, device)| {
                catalogs
                    .values(capability)
                    .any(|binding| binding["compatible"] == device.compatible)
            });
            let (id, _) = candidates.next().ok_or_else(|| {
                diagnostic(
                    origins,
                    "devices",
                    &format!("nuttx {capability} requires a device"),
                )
            })?;
            if candidates.next().is_some() {
                return Err(diagnostic(
                    origins,
                    &format!("aliases.{capability}"),
                    &format!("multiple {capability} devices require an explicit default alias"),
                ));
            }
            aliases.insert(capability.to_owned(), id.clone());
        }
    }
    validate_map_sizes(&resources, &devices, &aliases)
        .map_err(|e| diagnostic(origins, "resources", &e))?;
    let catalog = Catalog {
        binding: catalogs
            .values("imu")
            .map(|value| decode(value, "invalid IMU binding catalog"))
            .collect::<Result<Vec<_>>>()?,
    };
    let gnss_catalog = GnssCatalog {
        binding: catalogs
            .values("gnss")
            .map(|value| decode(value, "invalid GNSS binding catalog"))
            .collect::<Result<Vec<_>>>()?,
    };
    let (imu_implementation, gnss_implementation) = validate_graph(
        &catalog,
        &gnss_catalog,
        &resources,
        &devices,
        &aliases,
        origins,
    )?;
    let graph = Composition {
        version: header.version,
        mode: header.mode,
        resources,
        devices,
        aliases,
        imu_implementation,
        stack_bytes,
        gnss_implementation,
        gnss_stack_bytes,
        simulation,
    };
    for capability in ["imu", "gnss"] {
        graph
            .accessors(catalogs, capability)
            .map_err(|error| diagnostic(origins, "aliases", &error))?;
    }
    Ok(Some(graph))
}

fn validate_graph(
    catalog: &Catalog,
    gnss_catalog: &GnssCatalog,
    resources: &BTreeMap<String, Resource>,
    devices: &BTreeMap<String, Device>,
    aliases: &BTreeMap<String, String>,
    origins: &BTreeMap<String, PathBuf>,
) -> Result<(Option<String>, Option<String>)> {
    // Validate every physical instance, including devices not selected by an alias.
    // Discovery is limited to explicit [hal] capability keys, including "none".
    for (id, device) in devices {
        if !catalog
            .binding
            .iter()
            .any(|binding| binding.compatible == device.compatible)
            && !gnss_catalog
                .binding
                .iter()
                .any(|binding| binding.compatible == device.compatible)
        {
            return Err(diagnostic(
                origins,
                &format!("devices.{id}.compatible"),
                &format!(
                    "unsupported compatible/bus binding: {}; declare the owning capability in [hal] (provider may be none) and its package bindings in Cargo metadata",
                    device.compatible
                ),
            ));
        }
    }
    let mut endpoints: BTreeMap<&str, (&str, &Resource)> = BTreeMap::new();
    for (id, resource) in resources {
        let path = match resource {
            Resource::I2c { device, frequency } | Resource::Spi { device, frequency } => {
                // Resource validity is independent of the declared device
                // catalogs. A claiming device checks its own supported rates.
                if *frequency == 0 {
                    return Err(diagnostic(
                        origins,
                        &format!("resources.{id}.frequency"),
                        &format!("unsupported bus frequency {frequency}"),
                    ));
                }
                device
            }
            Resource::GpioInterrupt { device, .. } | Resource::Uart { device, .. } => device,
        };
        if let Some((previous_id, previous)) = endpoints.insert(path, (id, resource)) {
            if (previous != resource
                && !matches!(
                    (previous, resource),
                    (Resource::Spi { .. }, Resource::Spi { .. })
                ))
                || matches!(previous, Resource::Uart { .. })
                || matches!(resource, Resource::Uart { .. })
            {
                let (related, message) = match (previous, resource) {
                    (Resource::I2c { .. }, Resource::I2c { .. }) => (
                        format!("resources.{previous_id}.frequency"),
                        format!("conflicting I2C frequencies for endpoint {path}"),
                    ),
                    (Resource::Uart { .. }, _) | (_, Resource::Uart { .. }) => (
                        format!("resources.{previous_id}.kind"),
                        format!("exclusive UART endpoint {path} is claimed more than once"),
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
    let mut imu_implementation = None;
    let mut gnss_implementation = None;
    for (id, device) in devices {
        let is_gnss = gnss_catalog
            .binding
            .iter()
            .any(|b| b.compatible == device.compatible);
        if is_gnss {
            let binding = gnss_catalog
                .binding
                .iter()
                .find(|b| b.compatible == device.compatible && b.bus == "uart")
                .ok_or_else(|| {
                    diagnostic(
                        origins,
                        &format!("devices.{id}.compatible"),
                        "unsupported GNSS compatible/bus binding",
                    )
                })?;
            if device.address.is_some()
                || device.chip_select.is_some()
                || device.spi_mode.is_some()
                || device.wake.is_some()
            {
                return Err(diagnostic(
                    origins,
                    &format!("devices.{id}"),
                    "GNSS device does not accept address or wake",
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
            let Resource::Uart { device: path, baud } = bus else {
                return Err(diagnostic_related(
                    origins,
                    &format!("devices.{id}.bus"),
                    &[&format!("resources.{}.kind", device.bus)],
                    "requires a UART bus",
                ));
            };
            if !binding.bauds.contains(baud) {
                return Err(diagnostic(
                    origins,
                    &format!("resources.{}.baud", device.bus),
                    &format!("unsupported GNSS UART baud {baud}"),
                ));
            }
            if let Some(previous) =
                physical_claims.insert((path.as_str(), *baud), (id.as_str(), device.bus.as_str()))
            {
                return Err(diagnostic_related(
                    origins,
                    &format!("devices.{id}.bus"),
                    &[&format!("devices.{}.bus", previous.0)],
                    "conflicting exclusive UART device claim",
                ));
            }
            gnss_implementation = Some(binding.implementation.clone());
            continue;
        }
        let bus = resources.get(&device.bus).ok_or_else(|| {
            diagnostic_related(
                origins,
                &format!("devices.{id}.bus"),
                &[&format!("resources.{}", device.bus)],
                "references unknown bus",
            )
        })?;
        let (kind, path, frequency) = match bus {
            Resource::I2c { device, frequency } => ("i2c", device, frequency),
            Resource::Spi { device, frequency } => ("spi", device, frequency),
            _ => {
                return Err(diagnostic(
                    origins,
                    &format!("devices.{id}.bus"),
                    "requires an I2C or SPI bus",
                ))
            }
        };
        let binding = binding_for(catalog, &device.compatible, kind)
            .map_err(|error| diagnostic(origins, &format!("devices.{id}.compatible"), &error))?;
        let (claim, field) = if kind == "i2c" {
            if device.chip_select.is_some() || device.spi_mode.is_some() {
                return Err(diagnostic(
                    origins,
                    &format!("devices.{id}"),
                    "I2C device cannot have SPI settings",
                ));
            }
            let address = device.address.ok_or_else(|| {
                diagnostic(
                    origins,
                    &format!("devices.{id}.address"),
                    "IMU device requires address",
                )
            })?;
            if !binding.addresses.contains(&address) {
                return Err(diagnostic(
                    origins,
                    &format!("devices.{id}.address"),
                    "unsupported IMU address",
                ));
            }
            (u32::from(address), "address")
        } else {
            if device.address.is_some() {
                return Err(diagnostic(
                    origins,
                    &format!("devices.{id}.address"),
                    "SPI device cannot have an I2C address",
                ));
            }
            let select = device.chip_select.ok_or_else(|| {
                diagnostic(
                    origins,
                    &format!("devices.{id}.chip-select"),
                    "SPI device requires chip-select",
                )
            })?;
            let mode = device.spi_mode.ok_or_else(|| {
                diagnostic(
                    origins,
                    &format!("devices.{id}.spi-mode"),
                    "SPI device requires spi-mode",
                )
            })?;
            if mode > 3 || !binding.modes.contains(&mode) {
                return Err(diagnostic(
                    origins,
                    &format!("devices.{id}.spi-mode"),
                    "unsupported SPI mode",
                ));
            }
            (u32::from(select), "chip-select")
        };
        if device.wake.is_none() {
            return Err(diagnostic(
                origins,
                &format!("devices.{id}.wake"),
                "IMU device requires wake configuration",
            ));
        }
        if !binding.frequencies.contains(frequency) {
            return Err(diagnostic(
                origins,
                &format!("resources.{}.frequency", device.bus),
                "unsupported IMU bus frequency",
            ));
        }
        if let Some((previous_id, previous_bus)) =
            physical_claims.insert((path.as_str(), claim), (id, &device.bus))
        {
            return Err(diagnostic_related(
                origins,
                &format!("devices.{id}.{field}"),
                &[
                    &format!("devices.{previous_id}.{field}"),
                    &format!("devices.{id}.bus"),
                    &format!("devices.{previous_id}.bus"),
                    &format!("resources.{}.device", device.bus),
                    &format!("resources.{previous_bus}.device"),
                ],
                "conflicting physical device address claim",
            ));
        }
        if let Some(Wake::Interrupt { resource }) = &device.wake {
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
                Resource::I2c { .. } | Resource::Spi { .. } | Resource::Uart { .. } => {
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
    for (alias, alias_device) in aliases
        .iter()
        .filter(|(alias, _)| alias.as_str() == "imu" || alias.starts_with("imu_"))
    {
        let device = devices.get(alias_device).ok_or_else(|| {
            diagnostic_related(
                origins,
                &format!("aliases.{alias}"),
                &[&format!("devices.{alias_device}")],
                &format!("references unknown device {alias_device}"),
            )
        })?;
        let implementation = Some(
            binding_for(
                catalog,
                &device.compatible,
                match resources.get(&device.bus) {
                    Some(Resource::I2c { .. }) => "i2c",
                    Some(Resource::Spi { .. }) => "spi",
                    _ => return Err("invalid IMU bus".into()),
                },
            )?
            .implementation
            .clone(),
        );
        if alias == "imu" {
            imu_implementation = implementation;
        }
    }
    if let Some(alias_device) = aliases.get("gnss") {
        let device = devices.get(alias_device).ok_or_else(|| {
            diagnostic_related(
                origins,
                "aliases.gnss",
                &[&format!("devices.{alias_device}")],
                &format!("references unknown device {alias_device}"),
            )
        })?;
        let binding = gnss_catalog
            .binding
            .iter()
            .find(|binding| binding.compatible == device.compatible && binding.bus == "uart")
            .ok_or_else(|| {
                diagnostic(
                    origins,
                    "aliases.gnss",
                    "alias must select a supported GNSS device",
                )
            })?;
        gnss_implementation = Some(binding.implementation.clone());
    }
    Ok((imu_implementation, gnss_implementation))
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
            Resource::I2c { device, .. }
            | Resource::Spi { device, .. }
            | Resource::GpioInterrupt { device, .. }
            | Resource::Uart { device, .. } => {
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
    aliases: &BTreeMap<String, String>,
) -> Result<()> {
    if resources.len() > 64 || devices.len() > 64 || aliases.len() > 16 {
        return Err("composition graph exceeds bounded resource/device/alias counts".into());
    }
    Ok(())
}
fn validate_ids(
    resources: &BTreeMap<String, Resource>,
    devices: &BTreeMap<String, Device>,
    aliases: &BTreeMap<String, String>,
    origins: &BTreeMap<String, PathBuf>,
) -> Result<()> {
    for id in resources.keys() {
        valid_id(id).map_err(|error| diagnostic(origins, &format!("resources.{id}"), &error))?;
    }
    for id in devices.keys() {
        valid_id(id).map_err(|error| diagnostic(origins, &format!("devices.{id}"), &error))?;
        accessor_name(id).map_err(|error| diagnostic(origins, &format!("devices.{id}"), &error))?;
    }
    for (alias, id) in aliases {
        valid_id(id).map_err(|error| diagnostic(origins, &format!("aliases.{alias}"), &error))?;
    }
    Ok(())
}
fn alias_name(capability: &str, alias: &str) -> Option<Result<String>> {
    let primary = format!("primary_{capability}");
    if alias == capability {
        return Some(Ok(primary));
    }
    let name = alias.strip_prefix(capability)?.strip_prefix('_')?;
    if name == "primary" || name == primary {
        return Some(Err(format!(
            "use aliases.{capability} for the primary {capability} device"
        )));
    }
    Some(accessor_name(name).map(|()| name.to_owned()))
}

fn accessor_name(name: &str) -> Result<()> {
    // Emit exact Rust identifiers, without lossy normalization.
    if name.is_empty()
        || !name.as_bytes()[0].is_ascii_alphabetic()
        || !name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        || ["self", "super", "crate"].contains(&name)
    {
        return Err(format!("device/accessor name must be lowercase snake case and not self, super or crate: {name}"));
    }
    Ok(())
}
fn validate_aliases(aliases: &BTreeMap<String, String>) -> Result<()> {
    for alias in aliases.keys() {
        ["imu", "gnss"]
            .iter()
            .find_map(|capability| alias_name(capability, alias))
            .ok_or_else(|| format!("unsupported capability alias: {alias}"))??;
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

    fn fixture_catalogs() -> Catalogs {
        let sdk = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut catalogs = Catalogs::default();
        for capability in ["imu", "gnss"] {
            catalogs
                .insert(
                    capability,
                    crate::bindings::Package::load_capability(&sdk.join("platform"), capability)
                        .unwrap(),
                )
                .unwrap();
        }
        catalogs
    }

    fn profile(mode: &str, path: &str, address: u64, wake: Value) -> Value {
        json!({
            "composition": {"version":1, "mode":mode},
            "resources": {"i2c": {"kind":"i2c", "device":path, "frequency":400000}},
            "devices": {"imu": {"compatible":"bosch,bmi270", "bus":"i2c", "address":address, "wake":wake}},
            "aliases": {"imu":"imu"},
            "imu": {"stack-bytes":16384}
        })
    }

    fn gnss_profile() -> Value {
        json!({
            "composition":{"version":1,"mode":"hardware"},
            "resources":{"uart1":{"kind":"uart","device":"/dev//ttyS1","baud":38400}},
            "devices":{"navigation":{"compatible":"u-blox,neo-m9n","bus":"uart1"}},
            "aliases":{"gnss":"navigation"}
        })
    }

    #[test]
    fn gnss_names_aliases_and_independent_uarts_generate_canonical_instances() {
        let catalogs = fixture_catalogs();
        let mut data = gnss_profile();
        data["resources"]["backup_uart"] =
            json!({"kind":"uart", "device":"/dev/ttyS2", "baud":38400});
        data["devices"]["backup"] = json!({"compatible":"u-blox,neo-m9n", "bus":"backup_uart"});
        data["aliases"]["gnss_spare"] = json!("backup");
        let resolve = |data: &Value| {
            resolve_with_providers_and_origins(data, "none", "nuttx", &BTreeMap::new(), &catalogs)
        };
        let graph = resolve(&data).unwrap().unwrap();
        let names = graph.accessors(&catalogs, "gnss").unwrap();
        assert_eq!(names["primary_gnss"], "navigation");
        assert_eq!(names["spare"], "backup");
        assert_eq!(names["backup"], "backup");
        let generated =
            crate::configuration::render_instances(&graph, &catalogs, "gnss", 16384).unwrap();
        assert_eq!(generated.matches("declare_device!(").count(), 2);
        assert!(generated.contains("fn r#spare()"));
        let mut conflict = data.clone();
        conflict["aliases"]["gnss_backup"] = json!("navigation");
        assert!(resolve(&conflict).is_err());
        let mut unbound = data.clone();
        unbound["aliases"] = json!({"gnss_spare": "backup"});
        let error = resolve_with_providers_and_origins(
            &unbound,
            "none",
            "none",
            &BTreeMap::new(),
            &catalogs,
        )
        .unwrap_err();
        assert!(error.contains("aliases.gnss_spare"), "{error}");
        assert!(error.contains("physical gnss composition requires the nuttx provider"));
        data["resources"]["backup_uart"]["device"] = json!("/dev/ttyS1");
        assert!(resolve(&data).is_err());
    }

    #[test]
    fn named_imu_aliases_validate_identity_and_accessor_names() {
        let mut value = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        let catalogs = fixture_catalogs();
        let resolve = |value: &Value| {
            resolve_with_providers_and_origins(value, "nuttx", "none", &BTreeMap::new(), &catalogs)
        };
        value["aliases"]["imu_navigation"] = json!("imu");
        let graph = resolve(&value).unwrap().unwrap();
        assert_eq!(graph.aliases["imu_navigation"], graph.aliases["imu"]);
        assert_eq!(graph.devices.len(), 1);
        assert_eq!(
            graph.accessors(&catalogs, "imu").unwrap(),
            BTreeMap::from([
                ("imu".to_owned(), "imu"),
                ("navigation".to_owned(), "imu"),
                ("primary_imu".to_owned(), "imu")
            ])
        );
        value["aliases"]["imu_navigation"] = json!("absent");
        assert!(resolve(&value).is_err());
        value["aliases"]
            .as_object_mut()
            .unwrap()
            .remove("imu_navigation");
        for name in [
            "imu_",
            "imu_primary",
            "imu_self",
            "imu_super",
            "imu_crate",
            "imu_bad-name",
            "imu_1backup",
            "imu_Backup",
        ] {
            value["aliases"][name] = json!("imu");
            assert!(resolve(&value).is_err(), "{name}");
            value["aliases"].as_object_mut().unwrap().remove(name);
        }
        value["aliases"] = json!({"imu_backup":"imu"});
        assert_eq!(resolve(&value).unwrap().unwrap().aliases["imu"], "imu");
        value["devices"]["backup"] = value["devices"]["imu"].clone();
        value["devices"]["backup"]["address"] = json!(105);
        assert!(resolve(&value)
            .unwrap_err()
            .contains("explicit default alias"));
        value["aliases"]["imu"] = json!("imu");
        assert!(resolve(&value).unwrap_err().contains("accessor backup"));
        // An alias sharing a name with its own device is harmless and emitted once.
        value["aliases"]["imu_backup"] = json!("backup");
        assert_eq!(
            resolve(&value)
                .unwrap()
                .unwrap()
                .accessors(&catalogs, "imu")
                .unwrap()["backup"],
            "backup"
        );
    }

    #[test]
    fn aliases_are_optional_for_single_devices_and_legacy_roles_are_rejected() {
        let catalogs = fixture_catalogs();
        let mut imu = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        imu.as_object_mut().unwrap().remove("aliases");
        let graph = resolve(&imu, "nuttx", &catalogs).unwrap().unwrap();
        assert_eq!(graph.aliases["imu"], "imu");

        let mut gnss = gnss_profile();
        gnss.as_object_mut().unwrap().remove("aliases");
        let graph =
            resolve_with_providers_and_origins(&gnss, "none", "nuttx", &BTreeMap::new(), &catalogs)
                .unwrap()
                .unwrap();
        assert_eq!(graph.aliases["gnss"], "navigation");
        assert_eq!(graph.gnss().unwrap().baud, 38400);

        for name in ["bad-name", "1sensor", "Imu", "self", "super", "crate"] {
            let mut invalid = imu.clone();
            let device = invalid["devices"]
                .as_object_mut()
                .unwrap()
                .remove("imu")
                .unwrap();
            invalid["devices"][name] = device;
            assert!(resolve(&invalid, "nuttx", &catalogs).is_err(), "{name}");
        }
        imu["roles"] = json!({"imu":"imu"});
        assert!(resolve(&imu, "nuttx", &catalogs)
            .unwrap_err()
            .contains("rename [roles] to [aliases]"));
    }

    #[test]
    fn resolves_a_gnss_only_composition() {
        let versioned = resolve_with_providers_and_origins(
            &gnss_profile(),
            "none",
            "nuttx",
            &BTreeMap::new(),
            &fixture_catalogs(),
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            versioned.gnss().unwrap(),
            GnssBinding {
                implementation: "NeoM9n",
                path: "/dev/ttyS1",
                baud: 38400,
                stack_bytes: 16384
            }
        );
        assert_eq!(versioned.imu(), None);
    }

    #[test]
    fn composes_imu_and_gnss_aliases() {
        let mut joint = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        joint["resources"]["uart1"] = json!({"kind":"uart","device":"/dev/ttyS1","baud":38400});
        joint["devices"]["navigation"] = json!({"compatible":"u-blox,neo-m9n","bus":"uart1"});
        joint["aliases"]["gnss"] = json!("navigation");
        let binding = resolve_with_providers_and_origins(
            &joint,
            "nuttx",
            "nuttx",
            &BTreeMap::new(),
            &fixture_catalogs(),
        )
        .unwrap()
        .unwrap();
        assert!(binding.imu().is_some());
        assert_eq!(binding.gnss().unwrap().path, "/dev/ttyS1");
    }

    #[test]
    fn rejects_invalid_gnss_graphs_and_double_uart_claims() {
        let mut wrong_device = gnss_profile();
        wrong_device["devices"]["navigation"]["compatible"] = json!("unknown,receiver");
        assert!(resolve_with_providers_and_origins(
            &wrong_device,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &fixture_catalogs(),
        )
        .unwrap_err()
        .contains("unsupported"));
        let mut wrong_bus = gnss_profile();
        wrong_bus["resources"]["uart1"]["kind"] = json!("i2c");
        wrong_bus["resources"]["uart1"]["frequency"] = json!(400000);
        wrong_bus["resources"]["uart1"]
            .as_object_mut()
            .unwrap()
            .remove("baud");
        assert!(resolve_with_providers_and_origins(
            &wrong_bus,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &fixture_catalogs()
        )
        .unwrap_err()
        .contains("requires a UART"));
        let mut missing_bus = gnss_profile();
        missing_bus["devices"]["navigation"]["bus"] = json!("absent");
        assert!(resolve_with_providers_and_origins(
            &missing_bus,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &fixture_catalogs(),
        )
        .unwrap_err()
        .contains("unknown bus"));
        let mut baud = gnss_profile();
        baud["resources"]["uart1"]["baud"] = json!(12345);
        assert!(resolve_with_providers_and_origins(
            &baud,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &fixture_catalogs()
        )
        .unwrap_err()
        .contains("baud"));
        let mut alias = gnss_profile();
        alias["resources"]["uart_alias"] =
            json!({"kind":"uart","device":"/dev/ttyS1","baud":38400});
        alias["devices"]["another"] = json!({"compatible":"u-blox,neo-m9n","bus":"uart_alias"});
        assert!(resolve_with_providers_and_origins(
            &alias,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &fixture_catalogs()
        )
        .unwrap_err()
        .contains("exclusive UART"));
        let mut unsupported_alias = gnss_profile();
        unsupported_alias["aliases"]["radio"] = json!("navigation");
        assert!(resolve_with_providers_and_origins(
            &unsupported_alias,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &fixture_catalogs(),
        )
        .unwrap_err()
        .contains("unsupported capability alias"));
        assert!(resolve_with_providers_and_origins(
            &gnss_profile(),
            "none",
            "none",
            &BTreeMap::new(),
            &fixture_catalogs(),
        )
        .unwrap_err()
        .contains("nuttx provider"));
        let mut legacy_mix = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        legacy_mix["resources"]["uart1"] =
            json!({"kind":"uart","device":"/dev/ttyS1","baud":38400});
        legacy_mix["devices"]["navigation"] = json!({"compatible":"u-blox,neo-m9n","bus":"uart1"});
        legacy_mix["aliases"]["gnss"] = json!("navigation");
        legacy_mix["gnss"] = json!({"device":"/dev/ttyS1","baud":38400});
        assert!(resolve_with_providers_and_origins(
            &legacy_mix,
            "nuttx",
            "nuttx",
            &BTreeMap::new(),
            &fixture_catalogs(),
        )
        .unwrap_err()
        .contains("Only stack-bytes"));
    }

    #[test]
    fn rejects_unversioned_sensor_configuration_with_migration_guidance() {
        for (value, imu, gnss) in [
            (
                json!({"imu":{"driver":"bmi270", "device":"/dev/i2c0", "address":104, "frequency":400000}}),
                "nuttx",
                "none",
            ),
            (
                json!({"gnss":{"device":"/dev/ttyS1","baud":38400}}),
                "none",
                "nuttx",
            ),
        ] {
            let error = resolve_with_providers_and_origins(
                &value,
                imu,
                gnss,
                &BTreeMap::new(),
                &fixture_catalogs(),
            )
            .unwrap_err();
            assert!(error.contains("composition version 1"), "{error}");
        }
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
        let composition = resolve(&value, "nuttx", &fixture_catalogs())
            .unwrap()
            .unwrap();
        assert_eq!(composition.mode(), Mode::Qualification);
        assert_eq!(composition.imu().unwrap().irq_path, Some("/dev/gpio0"));
        assert_eq!(composition.imu().unwrap().implementation, "Bmi270");
    }

    #[test]
    fn rejects_mixed_legacy_fields_unknown_keys_and_orphan_graphs() {
        let mut mixed = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        mixed["imu"]["driver"] = json!("bmi270");
        assert!(resolve(&mixed, "nuttx", &fixture_catalogs()).is_err());
        let mut extra = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        extra["devices"]["imu"]["extra"] = json!(true);
        assert!(resolve(&extra, "nuttx", &fixture_catalogs()).is_err());
        let mut stale_wake = profile(
            "hardware",
            "/dev/i2c0",
            104,
            json!({"kind":"periodic", "resource":"irq"}),
        );
        assert!(resolve(&stale_wake, "nuttx", &fixture_catalogs()).is_err());
        stale_wake["composition"]["version"] = json!(2);
        assert!(resolve(&stale_wake, "nuttx", &fixture_catalogs())
            .unwrap_err()
            .contains("version"));
        assert!(
            resolve(&json!({"resources":{}}), "none", &fixture_catalogs())
                .unwrap_err()
                .contains("version header")
        );
        let nested_legacy_shape = json!({
            "composition": {
                "version": 1,
                "mode": "hardware",
                "resources": {"i2c": {"kind":"i2c", "device":"/dev/i2c0", "frequency":400000}},
                "devices": {"imu": {"compatible":"bosch,bmi270", "bus":"i2c", "address":104, "wake":{"kind":"periodic"}}},
                "aliases": {"imu":"imu"}
            },
            "imu": {"stack-bytes":16384}
        });
        assert!(resolve(&nested_legacy_shape, "nuttx", &fixture_catalogs()).is_err());
    }

    #[test]
    fn normalizes_paths_for_alias_conflict_detection_and_serialization() {
        let mut conflict = profile("hardware", "/dev//i2c0", 104, json!({"kind":"periodic"}));
        conflict["devices"]["other"] = json!({"compatible":"bosch,bmi270", "bus":"i2c", "address":104, "wake":{"kind":"periodic"}});
        assert!(resolve(&conflict, "nuttx", &fixture_catalogs())
            .unwrap_err()
            .contains("address claim"));
        let valid = profile("hardware", "/dev//i2c0", 105, json!({"kind":"periodic"}));
        assert_eq!(
            resolve(&valid, "nuttx", &fixture_catalogs())
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
        assert!(resolve(&distinct, "nuttx", &fixture_catalogs()).is_ok());

        let mut speed = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        speed["resources"]["alias"] =
            json!({"kind":"i2c", "device":"/dev//i2c0", "frequency":100000});
        assert!(resolve(&speed, "nuttx", &fixture_catalogs())
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
        assert!(resolve(&bad_irq, "nuttx", &fixture_catalogs()).is_err());

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
        assert!(resolve(&aliased_irq, "nuttx", &fixture_catalogs())
            .unwrap_err()
            .contains("exclusive IRQ"));

        let physical = profile("simulation", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        assert!(resolve(&physical, "nuttx", &fixture_catalogs())
            .unwrap_err()
            .contains("physical IMU"));

        let mut mixed = distinct;
        mixed["hal"] = json!({"imu":"nuttx","gnss":"mock"});
        assert!(resolve(&mixed, "nuttx", &fixture_catalogs())
            .unwrap_err()
            .contains("mock HAL"));
    }

    #[test]
    fn replacement_lsm6dso_requires_its_own_address_and_periodic_wakes() {
        let mut replacement = profile("hardware", "/dev/i2c1", 106, json!({"kind":"periodic"}));
        replacement["devices"]["imu"]["compatible"] = json!("st,lsm6dso");
        let resolved = resolve(&replacement, "nuttx", &fixture_catalogs())
            .unwrap()
            .unwrap();
        let binding = resolved.imu().unwrap();
        assert_eq!(binding.implementation, "Lsm6dso");
        assert_eq!(
            (binding.path, binding.transport),
            ("/dev/i2c1", ImuTransport::I2c { address: 106 })
        );

        replacement["devices"]["imu"]["address"] = json!(104);
        assert!(resolve(&replacement, "nuttx", &fixture_catalogs())
            .unwrap_err()
            .contains("address"));
        replacement["devices"]["imu"]["address"] = json!(106);
        replacement["devices"]["imu"]["wake"] = json!({"kind":"interrupt","resource":"irq"});
        assert!(resolve(&replacement, "nuttx", &fixture_catalogs())
            .unwrap_err()
            .contains("does not support interrupt wake"));
    }

    #[test]
    fn spi_selection_validates_transport_fields_and_physical_claims() {
        let mut value = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        value["resources"]["spi"] =
            json!({"kind":"spi","device":"/dev/spi2","frequency":4_000_000});
        value["devices"]["imu"] = json!({"compatible":"bosch,bmi270","bus":"spi","chip-select":0,"spi-mode":3,"wake":{"kind":"periodic"}});
        let catalogs = fixture_catalogs();
        for (compatible, implementation) in
            [("bosch,bmi270", "Bmi270Spi"), ("st,lsm6dso", "Lsm6dsoSpi")]
        {
            value["devices"]["imu"]["compatible"] = json!(compatible);
            let graph = resolve(&value, "nuttx", &catalogs).unwrap().unwrap();
            let binding = graph.imu().unwrap();
            assert_eq!(binding.implementation, implementation);
            assert_eq!(
                binding.transport,
                ImuTransport::Spi {
                    chip_select: 0,
                    mode: 3
                }
            );
        }
        for (key, invalid, message) in [
            ("chip-select", json!(65536), "u16"),
            ("spi-mode", json!(1), "unsupported SPI mode"),
            ("address", json!(106), "I2C address"),
        ] {
            let mut bad = value.clone();
            bad["devices"]["imu"][key] = invalid;
            assert!(resolve(&bad, "nuttx", &catalogs)
                .unwrap_err()
                .contains(message));
        }
        for key in ["chip-select", "spi-mode"] {
            let mut bad = value.clone();
            bad["devices"]["imu"].as_object_mut().unwrap().remove(key);
            assert!(resolve(&bad, "nuttx", &catalogs).unwrap_err().contains(key));
        }
        let mut bad = value.clone();
        bad["resources"]["spi"]["frequency"] = json!(9_000_000);
        assert!(resolve(&bad, "nuttx", &catalogs)
            .unwrap_err()
            .contains("frequency"));
        value["resources"]["alias"] =
            json!({"kind":"spi","device":"/dev/./spi2","frequency":1_000_000});
        value["devices"]["other"] = value["devices"]["imu"].clone();
        value["devices"]["other"]["bus"] = json!("alias");
        assert!(resolve(&value, "nuttx", &catalogs)
            .unwrap_err()
            .contains("conflicting physical device"));
        value["devices"]["other"]["chip-select"] = json!(1);
        assert!(resolve(&value, "nuttx", &catalogs).is_ok());
        value["devices"]["other"]["bus"] = json!("i2c");
        assert!(resolve(&value, "nuttx", &catalogs)
            .unwrap_err()
            .contains("I2C device cannot have SPI"));
    }

    #[test]
    fn validates_unselected_devices_and_stable_serialization() {
        let mut invalid = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        invalid["devices"]["other"] = json!({"compatible":"bosch,bmi270", "bus":"i2c", "address":200, "wake":{"kind":"periodic"}});
        assert!(resolve(&invalid, "nuttx", &fixture_catalogs())
            .unwrap_err()
            .contains("address"));
        let a = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        let b = json!({"imu":{"stack-bytes":16384},"aliases":{"imu":"imu"},"devices":{"imu":{"wake":{"kind":"periodic"},"address":104,"bus":"i2c","compatible":"bosch,bmi270"}},"resources":{"i2c":{"frequency":400000,"device":"/dev/i2c0","kind":"i2c"}},"composition":{"mode":"hardware","version":1}});
        let a = serde_json::to_vec(&resolve(&a, "nuttx", &fixture_catalogs()).unwrap().unwrap())
            .unwrap();
        let b = serde_json::to_vec(&resolve(&b, "nuttx", &fixture_catalogs()).unwrap().unwrap())
            .unwrap();
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
                "devices.imu.wake.resource".into(),
                PathBuf::from("base.toml"),
            ),
            (
                "resources.ready".into(),
                PathBuf::from("overlays/remove-ready.toml"),
            ),
        ]);
        let error =
            resolve_with_origins(&value, "nuttx", &origins, &fixture_catalogs()).unwrap_err();
        assert!(error.contains("devices.imu.wake.resource"));
        assert!(error.contains("resources.ready changed by overlays/remove-ready.toml"));
    }

    #[test]
    fn gnss_only_catalogs_allow_an_unclaimed_i2c_resource() {
        let mut value = gnss_profile();
        value["resources"]["spare"] = json!({"kind":"i2c","device":"/dev/i2c0","frequency":400000});
        let sdk = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut catalogs = Catalogs::default();
        catalogs
            .insert(
                "gnss",
                crate::bindings::Package::load_capability(&sdk.join("platform"), "gnss").unwrap(),
            )
            .unwrap();
        assert!(catalogs.package("imu").is_none());
        let graph = super::resolve_with_providers_and_origins(
            &value,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &catalogs,
        )
        .unwrap()
        .unwrap();
        assert_eq!(graph.gnss().unwrap().path, "/dev/ttyS1");

        value["resources"]["spare"]["frequency"] = json!(0);
        let error = super::resolve_with_providers_and_origins(
            &value,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &catalogs,
        )
        .unwrap_err();
        assert!(error.contains("unsupported bus frequency 0"), "{error}");

        let mut claimed = profile("hardware", "/dev/i2c0", 104, json!({"kind":"periodic"}));
        claimed["resources"]["i2c"]["frequency"] = json!(12345);
        assert!(resolve(&claimed, "nuttx", &fixture_catalogs())
            .unwrap_err()
            .contains("unsupported IMU bus frequency"));
    }

    #[test]
    fn unselected_devices_require_their_capability_catalog() {
        let mut value = gnss_profile();
        value["resources"]["i2c"] = json!({"kind":"i2c","device":"/dev/i2c0","frequency":400000});
        value["devices"]["imu"] = json!({
            "compatible":"bosch,bmi270","bus":"i2c","address":104,
            "wake":{"kind":"periodic"}
        });
        let sdk = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut catalogs = Catalogs::default();
        catalogs
            .insert(
                "gnss",
                crate::bindings::Package::load_capability(&sdk.join("platform"), "gnss").unwrap(),
            )
            .unwrap();
        let error = super::resolve_with_providers_and_origins(
            &value,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &catalogs,
        )
        .unwrap_err();
        assert!(error.contains("unsupported compatible/bus"), "{error}");
        assert!(error.contains("[hal]"), "{error}");
        catalogs
            .insert(
                "imu",
                crate::bindings::Package::load_capability(&sdk.join("platform"), "imu").unwrap(),
            )
            .unwrap();
        assert!(super::resolve_with_providers_and_origins(
            &value,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &catalogs,
        )
        .is_ok());
        value["devices"]["imu"]["address"] = json!(0x70);
        assert!(super::resolve_with_providers_and_origins(
            &value,
            "none",
            "nuttx",
            &BTreeMap::new(),
            &catalogs,
        )
        .unwrap_err()
        .contains("unsupported"));
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
        let composition = resolve(&simulation_profile(), "simulation", &fixture_catalogs())
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
        assert!(encoded.get("imu_implementation").is_none());
        assert_eq!(encoded["simulation"]["endpoints"], json!(["imu", "gnss"]));
    }

    #[test]
    fn simulation_rejects_ambiguous_or_physical_composition() {
        let mut value = simulation_profile();
        value["simulation"]["endpoints"] = json!([]);
        assert!(resolve(&value, "simulation", &fixture_catalogs())
            .unwrap_err()
            .contains("at least one"));

        let mut value = simulation_profile();
        value["simulation"]["endpoints"] = json!(["imu", "imu"]);
        assert!(resolve(&value, "simulation", &fixture_catalogs())
            .unwrap_err()
            .contains("unique"));

        let mut value = simulation_profile();
        value["simulation"]["endpoints"] = json!(["camera"]);
        assert!(resolve(&value, "simulation", &fixture_catalogs())
            .unwrap_err()
            .contains("unsupported simulation endpoint"));

        for key in ["resources", "devices", "aliases", "imu", "gnss"] {
            let mut value = simulation_profile();
            value[key] = json!({});
            assert!(
                resolve(&value, "simulation", &fixture_catalogs())
                    .unwrap_err()
                    .contains("cannot declare physical"),
                "{key}"
            );
        }

        let mut value = simulation_profile();
        value["hal"]["imu"] = json!("nuttx");
        let error = resolve(&value, "simulation", &fixture_catalogs()).unwrap_err();
        assert!(error.contains("simulation"), "{error}");

        let mut value = simulation_profile();
        value["hal"]["camera"] = json!("mock");
        assert!(resolve(&value, "simulation", &fixture_catalogs())
            .unwrap_err()
            .contains("exactly match"));
    }

    #[test]
    fn simulation_table_is_exclusive_to_simulation_mode() {
        let mut value = simulation_profile();
        value["composition"]["mode"] = json!("hardware");
        value["hal"] = json!({"imu":"none", "gnss":"none"});
        assert!(resolve(&value, "simulation", &fixture_catalogs())
            .unwrap_err()
            .contains("valid only in simulation mode"));

        let mut value = simulation_profile();
        value.as_object_mut().unwrap().remove("simulation");
        assert!(resolve(&value, "simulation", &fixture_catalogs())
            .unwrap_err()
            .contains("requires [simulation]"));

        let value = json!({"hal":{"imu":"simulation"}});
        assert!(resolve(&value, "simulation", &fixture_catalogs())
            .unwrap_err()
            .contains("require composition.mode = simulation"));
    }
}
