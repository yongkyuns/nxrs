//! Signed-image geometry and NuttX configuration. This module never accesses a device.
use crate::{profile::Profile, Result};
use serde_json::Value;
use std::{collections::BTreeMap, path::Path};
pub const MCUBOOT_REVISION: &str = "8a07053d42e592c85cb35b79c4de1b7749943387";
pub const HAL_REVISION: &str = "911dbec8e4a92e70056b58a3d2b0d965b8b7bcc9";
#[derive(Clone, Debug)]
pub struct Layout {
    pub flash: u64,
    pub primary: u64,
    pub secondary: u64,
    pub slot: u64,
    pub scratch: u64,
    pub scratch_size: u64,
    pub header: u64,
    pub align: u64,
    pub erase: u64,
}
impl Layout {
    pub fn parse(v: &Value) -> Result<Self> {
        let fields = [
            "flash-size",
            "primary-offset",
            "secondary-offset",
            "slot-size",
            "scratch-offset",
            "scratch-size",
            "header-size",
            "write-align",
            "erase-size",
        ];
        let obj = v
            .as_object()
            .filter(|m| m.len() == fields.len() && fields.iter().all(|s| m.contains_key(*s)))
            .ok_or("MCUboot requires explicit flash/slot/scratch/header/alignment geometry")?;
        let mut n = BTreeMap::new();
        for (k, v) in obj {
            let value = v
                .as_u64()
                .filter(|v| *v <= u32::MAX as u64 && (*v > 0 || k.ends_with("-offset")))
                .ok_or("invalid MCUboot byte count/offset")?;
            n.insert(k.as_str(), value);
        }
        let l = Self {
            flash: n["flash-size"],
            primary: n["primary-offset"],
            secondary: n["secondary-offset"],
            slot: n["slot-size"],
            scratch: n["scratch-offset"],
            scratch_size: n["scratch-size"],
            header: n["header-size"],
            align: n["write-align"],
            erase: n["erase-size"],
        };
        if ![1, 2, 4, 8, 16, 32].contains(&l.align)
            || l.header < 32
            || !l.header.is_multiple_of(l.align)
            || !l.erase.is_power_of_two()
            || !l.erase.is_multiple_of(l.align)
        {
            return Err("invalid MCUboot alignment/header/erase size".into());
        }
        let mut regions = [
            (l.primary, l.slot),
            (l.secondary, l.slot),
            (l.scratch, l.scratch_size),
        ];
        regions.sort();
        let mut end = 0;
        for (offset, size) in regions {
            if offset % l.erase != 0 || size % l.erase != 0 || offset < end {
                return Err("MCUboot regions must be disjoint and sector aligned".into());
            }
            end = offset + size;
        }
        if end > l.flash || l.slot <= l.header {
            return Err("MCUboot geometry exceeds flash or leaves no image space".into());
        }
        Ok(l)
    }
}
pub fn image_class(p: &Profile) -> Result<[u8; 16]> {
    let s = p.data["image-class"]
        .as_str()
        .ok_or("MCUboot product requires an image-class UUID")?;
    if s.len() != 36
        || ![8, 13, 18, 23].iter().all(|i| s.as_bytes()[*i] == b'-')
        || s.bytes().enumerate().any(|(i, b)| {
            ![8, 13, 18, 23].contains(&i) && !b.is_ascii_digit() && !(b'a'..=b'f').contains(&b)
        })
    {
        return Err("image-class must be a nonzero canonical UUID".into());
    }
    let compact = s.replace('-', "");
    let mut bytes = [0; 16];
    for (i, b) in bytes.iter_mut().enumerate() {
        *b = u8::from_str_radix(&compact[i * 2..i * 2 + 2], 16).map_err(|e| e.to_string())?
    }
    if bytes.iter().all(|b| *b == 0) {
        return Err("image-class must be nonzero".into());
    }
    Ok(bytes)
}
pub fn protected(p: &Profile) -> Result<bool> {
    match p.data.get("hardware-protection") {
        None => Ok(false),
        Some(v) => v
            .as_bool()
            .ok_or("hardware-protection must be boolean".into()),
    }
}
pub fn application_config(p: &Profile) -> Result<BTreeMap<String, String>> {
    let b = Layout::parse(&p.data["mcuboot"])?;
    image_class(p)?;
    let protected = protected(p)?;
    if p.data["target"] != "xtensa-esp32s3-nuttx" {
        return Err("MCUboot build wiring is not qualified for this NuttX target".into());
    }
    if b.flash != 0x1000000
        || b.primary < 0x20000
        || b.erase != 4096
        || b.slot / b.erase != 512
        || b.align != if protected { 32 } else { 4 }
    {
        return Err(
            "ESP32-S3 MCUboot geometry/alignment does not match qualified flash layout".into(),
        );
    }
    if protected && p.binding("boot") != "none" {
        return Err("encrypted update control requires interrupted-write qualification".into());
    }
    let mut v: BTreeMap<String, String> = [
        (
            "ESP32S3_OTA_PRIMARY_SLOT_OFFSET",
            format!("{:#x}", b.primary),
        ),
        (
            "ESP32S3_OTA_SECONDARY_SLOT_OFFSET",
            format!("{:#x}", b.secondary),
        ),
        ("ESP32S3_OTA_SLOT_SIZE", format!("{:#x}", b.slot)),
        ("ESP32S3_OTA_SCRATCH_OFFSET", format!("{:#x}", b.scratch)),
        ("ESP32S3_OTA_SCRATCH_SIZE", format!("{:#x}", b.scratch_size)),
        ("ESP32S3_APP_MCUBOOT_HEADER_SIZE", b.header.to_string()),
        ("ESP32S3_MCUBOOT_VERSION", format!("\"{MCUBOOT_REVISION}\"")),
    ]
    .into_iter()
    .map(|(k, v)| (k.into(), v))
    .collect();
    if p.binding("boot") == "nuttx" || protected {
        v.extend([
            ("ESP32S3_SPIFLASH".into(), "y".into()),
            ("ESP32S3_SPIFLASH_AUTO_BRINGUP".into(), "n".into()),
            (
                "ESP32S3_STORAGE_MTD_OFFSET".into(),
                format!("{:#x}", b.scratch + b.scratch_size),
            ),
        ]);
    }
    if p.binding("boot") == "nuttx" {
        v.insert("BOOT_MCUBOOT".into(), "y".into());
        v.insert("MCUBOOT_VERSION".into(), format!("\"{MCUBOOT_REVISION}\""));
    }
    if p.binding("config") == "nuttx" {
        let offset = b.scratch + b.scratch_size;
        if p.binding("boot") != "nuttx"
            || protected
            || offset < 0x20000
            || offset + 0x2000 > b.flash
            || [b.primary, b.secondary]
                .iter()
                .any(|start| offset.max(*start) < (offset + 0x2000).min(start + b.slot))
        {
            return Err("config partition requires separate unencrypted MCUboot storage".into());
        }
        for (k, value) in [
            ("MTD_CONFIG_NVS", "y"),
            ("MTD_CONFIG_NONE", "n"),
            ("MTD_CONFIG_NAMED", "y"),
            ("MTD_CONFIG_NAME_LEN", "32"),
            ("ESP32S3_STORAGE_MTD_SIZE", "0x2000"),
        ] {
            v.insert(k.into(), value.into());
        }
    }
    if p.binding("watchdog") == "nuttx" {
        if p.binding("boot") != "nuttx" {
            return Err("inherited watchdog requires boot control".into());
        }
        for k in ["WATCHDOG", "ESP32S3_RWDT", "ESP32S3_RWDT_INHERIT"] {
            v.insert(k.into(), "y".into());
        }
    }
    if protected {
        v.insert("ESP32S3_OTA_PARTITION_ENCRYPT".into(), "y".into());
        v.insert("ESP32S3_SPIFLASH_MTD_BLKSIZE".into(), "32".into());
    }
    Ok(v)
}
pub fn watchdog_timeout(p: &Profile) -> Result<u64> {
    p.data.get("boot-watchdog-ms").map_or(Ok(9000), |v| {
        v.as_u64()
            .filter(|v| (1..=300000).contains(v))
            .ok_or("invalid boot-watchdog-ms".into())
    })
}
pub fn init_config(
    p: &Profile,
    command: &str,
    priority: u64,
    stack: u64,
) -> Result<BTreeMap<String, String>> {
    let Some(init) = p.data.get("init") else {
        return Ok(BTreeMap::new());
    };
    let obj = init.as_object().ok_or("invalid init table")?;
    if obj
        .keys()
        .any(|s| !["args", "console", "network", "priority", "stack-bytes"].contains(&s.as_str()))
        || !command
            .chars()
            .enumerate()
            .all(|(i, c)| c == '_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit()))
        || command.is_empty()
    {
        return Err("invalid init entry/fields".into());
    }
    let priority = init
        .get("priority")
        .unwrap_or(&Value::from(priority))
        .as_u64()
        .filter(|n| (1..=255).contains(n))
        .ok_or("invalid init priority")?;
    let stack = init
        .get("stack-bytes")
        .unwrap_or(&Value::from(stack))
        .as_u64()
        .filter(|n| *n > 0)
        .ok_or("invalid init stack")?;
    let args = init.get("args").cloned().unwrap_or(serde_json::json!([]));
    let args = args
        .as_array()
        .filter(|a| a.len() <= 16)
        .ok_or("invalid bounded init args")?;
    let mut encoded = vec![];
    for a in args {
        let s = a
            .as_str()
            .filter(|s| {
                s.len() <= 128
                    && s.bytes()
                        .all(|b| (32..=126).contains(&b) && b != b'"' && b != b'\\')
            })
            .ok_or("invalid bounded init argument")?;
        encoded.push(serde_json::to_string(s).unwrap());
    }
    let mut v: BTreeMap<String, String> = [
        ("INIT_ENTRY".into(), "y".into()),
        (
            "INIT_ENTRYPOINT".into(),
            serde_json::to_string(&format!("{command}_main")).unwrap(),
        ),
        (
            "INIT_ARGS".into(),
            serde_json::to_string(&encoded.join(",")).unwrap(),
        ),
        ("INIT_PRIORITY".into(), priority.to_string()),
        ("INIT_STACKSIZE".into(), stack.to_string()),
    ]
    .into_iter()
    .collect();
    if let Some(console) = init.get("console") {
        if console != "uart0" {
            return Err("unsupported init console".into());
        }
        v.insert("UART0_SERIAL_CONSOLE".into(), "y".into());
        v.insert("OTHER_SERIAL_CONSOLE".into(), "n".into());
    }
    if p.data.get("boot-watchdog-ms").is_some()
        && (p.data.get("mcuboot").is_none()
            || p.binding("boot") != "nuttx"
            || p.binding("watchdog") != "nuttx")
    {
        return Err("inherited boot watchdog requires MCUboot and boot/watchdog control".into());
    }
    if let Some(network) = init.get("network") {
        if !["external", "on-acquire"].iter().any(|s| network == *s) {
            return Err("unsupported network initialization policy".into());
        }
        if network == "on-acquire" {
            v.insert("NETINIT_THREAD".into(), "n".into());
            v.insert("NETUTILS_NETINIT".into(), "y".into());
        }
    }
    Ok(v)
}
pub fn bootloader_config(p: &Profile, root: &Path) -> Result<String> {
    application_config(p)?;
    let path = root
        .to_str()
        .ok_or("public root needs a UTF-8 absolute path")?;
    if !root.is_absolute()
        || path
            .chars()
            .any(|c| ['\n', '\r', ';', '"', '\\'].contains(&c))
    {
        return Err("public root needs an absolute path without CMake delimiters".into());
    }
    let b = Layout::parse(&p.data["mcuboot"])?;
    let mut v = vec![
        ("NON_OS_BUILD", "1".into()),
        ("CONFIG_ESPRESSIF_BOOTLOADER_MCUBOOT", "1".into()),
        ("CONFIG_ESP_BOOTLOADER_OFFSET", "0x0".into()),
        ("CONFIG_ESP_BOOTLOADER_SIZE", "0xf000".into()),
        (
            "CONFIG_ESP_IMAGE0_PRIMARY_START_ADDRESS",
            format!("{:#x}", b.primary),
        ),
        (
            "CONFIG_ESP_IMAGE0_SECONDARY_START_ADDRESS",
            format!("{:#x}", b.secondary),
        ),
        ("CONFIG_ESP_APPLICATION_SIZE", format!("{:#x}", b.slot)),
        ("CONFIG_ESP_SCRATCH_OFFSET", format!("{:#x}", b.scratch)),
        ("CONFIG_ESP_SCRATCH_SIZE", format!("{:#x}", b.scratch_size)),
    ];
    for k in [
        "CONFIG_LIBC_NEWLIB",
        "CONFIG_IDF_TARGET_ARCH_XTENSA",
        "CONFIG_BOOTLOADER_FLASH_XMC_SUPPORT",
    ] {
        v.push((k, "1".into()))
    }
    v.extend([
        ("CONFIG_BOOTLOADER_LOG_LEVEL", "3".into()),
        ("CONFIG_BOOTLOADER_WDT_ENABLE", "1".into()),
        (
            "CONFIG_BOOTLOADER_WDT_TIME_MS",
            watchdog_timeout(p)?.to_string(),
        ),
        ("CONFIG_ESP_MCUBOOT_WDT_ENABLE", "1".into()),
        ("CONFIG_ESP_CONSOLE_UART", "1".into()),
        ("CONFIG_ESP_CONSOLE_UART_NUM", "0".into()),
        ("CONFIG_ESP_CONSOLE_ROM_SERIAL_PORT_NUM", "0".into()),
    ]);
    for k in [
        "CONFIG_ESP_SIGN_EC256",
        "CONFIG_ESP_USE_TINYCRYPT",
        "CONFIG_ESP_DOWNGRADE_PREVENTION",
        "CONFIG_ESP_DOWNGRADE_PREVENTION_SECURITY_COUNTER",
    ] {
        v.push((k, "1".into()))
    }
    v.extend([
        ("CONFIG_ESP_SIGN_KEY_FILE", path.into()),
        ("MCUBOOT_UUID_CID", "1".into()),
        (
            "CONFIG_ESP_IMAGE_CLASS_ID",
            image_class(p)?
                .iter()
                .map(|b| format!("0x{b:02x}"))
                .collect::<Vec<_>>()
                .join(","),
        ),
    ]);
    if protected(p)? {
        for k in [
            "CONFIG_SECURE_SIGNED_ON_BOOT",
            "CONFIG_SECURE_SIGNED_APPS_RSA_SCHEME",
            "CONFIG_SECURE_BOOT",
            "CONFIG_SECURE_BOOT_V2_ENABLED",
            "CONFIG_SECURE_BOOT_SUPPORTS_RSA",
            "CONFIG_SECURE_FLASH_ENC_ENABLED",
            "CONFIG_SECURE_FLASH_ENCRYPTION_MODE_RELEASE",
            "CONFIG_SECURE_FLASH_REQUIRE_ALREADY_ENABLED",
            "CONFIG_SECURE_BOOT_REQUIRE_ALREADY_ENABLED",
            "CONFIG_EFUSE_WRITES_FORBIDDEN",
        ] {
            v.push((k, "1".into()))
        }
    }
    Ok(v.iter().map(|(k, v)| format!("{k}={v}\n")).collect())
}

pub struct Signing {
    pub key: std::path::PathBuf,
    pub root: std::path::PathBuf,
    pub version: String,
    pub counter: u32,
}
impl Signing {
    pub fn environment() -> Result<Self> {
        fn env(name: &str) -> Result<String> {
            std::env::var(name).map_err(|_| format!("MCUboot build requires {name}"))
        }
        let s = Self {
            key: env("NXRS_FIRMWARE_SIGNING_KEY")?.into(),
            root: env("NXRS_FIRMWARE_VERIFY_KEY")?.into(),
            version: env("NXRS_FIRMWARE_VERSION")?,
            counter: env("NXRS_FIRMWARE_SECURITY_COUNTER")?
                .parse()
                .map_err(|_| "security counter must be unsigned 32-bit")?,
        };
        if !s.key.is_file() || !s.root.is_file() {
            return Err("signing key and independent verification root must exist".into());
        }
        Ok(s)
    }
    pub fn sign(&self, p: &Profile, input: &Path, out: &Path, confirm: bool) -> Result<()> {
        use std::process::Command;
        let layout = Layout::parse(&p.data["mcuboot"])?;
        if p.data.get("image-class").is_some() {
            image_class(p)?;
        }
        let parent = out
            .parent()
            .ok_or("image output needs a parent")?
            .canonicalize()
            .map_err(|e| e.to_string())?;
        let resolved = parent.join(out.file_name().ok_or("image output needs a name")?);
        for path in [input, &self.key, &self.root] {
            if !path.is_file() || path.canonicalize().map_err(|e| e.to_string())? == resolved {
                return Err("signed image output must not replace an input or key".into());
            }
        }
        if crate::output(Command::new("imgtool").arg("version"))?.trim() != "2.2.0" {
            return Err("MCUboot requires pinned imgtool 2.2.0".into());
        }
        let temporary = crate::TempDir::new(&parent)?;
        let candidate = temporary.0.join("image.bin");
        let mut cmd = Command::new("imgtool");
        cmd.args(["sign", "-k"]).arg(&self.key).args([
            "-v",
            &self.version,
            "-s",
            &self.counter.to_string(),
            "-H",
            &layout.header.to_string(),
            "--pad-header",
            "-S",
            &layout.slot.to_string(),
            "--align",
            &layout.align.to_string(),
            "--max-align",
            &layout.align.max(8).to_string(),
            "--max-sectors",
            &(layout.slot / layout.erase).to_string(),
        ]);
        if let Some(class) = p.data["image-class"].as_str() {
            cmd.args(["--cid", class]);
        }
        if confirm {
            cmd.args(["--pad", "--confirm"]);
        }
        crate::output(cmd.arg(input).arg(&candidate))?;
        crate::output(
            Command::new("imgtool")
                .args(["verify", "-k"])
                .arg(&self.root)
                .arg(&candidate),
        )?;
        std::fs::rename(candidate, resolved).map_err(|e| e.to_string())
    }
}

/// Verify a public root using the external cryptographic tool, never parse a private key as a root.
pub(crate) struct PublicRoot {
    pub pem: Vec<u8>,
    pub source_sha256: String,
}
fn openssl_input(args: &[&str], input: &[u8]) -> Result<Vec<u8>> {
    use std::{
        io::Write,
        process::{Command, Stdio},
    };
    let mut child = Command::new("openssl")
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let written = child
        .stdin
        .take()
        .unwrap()
        .write_all(input)
        .map_err(|e| e.to_string());
    if written.is_err() {
        let _ = child.kill();
    }
    let output = child.wait_with_output().map_err(|e| e.to_string())?;
    written?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).into_owned());
    }
    Ok(output.stdout)
}
pub(crate) fn public_root(root: &Path, rsa: bool) -> Result<PublicRoot> {
    let original = std::fs::read(root).map_err(|e| e.to_string())?;
    let detail = openssl_input(&["pkey", "-pubin", "-text", "-noout"], &original).map_err(|e| {
        format!(
            "expected {} PUBLIC key: {e}",
            if rsa { "RSA-3072" } else { "EC P-256" }
        )
    })?;
    let detail = std::str::from_utf8(&detail).map_err(|e| e.to_string())?;
    if !(if rsa {
        detail.contains("Public-Key: (3072 bit)")
    } else {
        detail.contains("ASN1 OID: prime256v1")
    }) {
        return Err(if rsa {
            "expected RSA-3072 PUBLIC key"
        } else {
            "expected EC P-256 PUBLIC key"
        }
        .into());
    }
    Ok(PublicRoot {
        pem: openssl_input(&["pkey", "-pubin", "-pubout"], &original)?,
        source_sha256: crate::hash_bytes(&original),
    })
}
pub fn build_loader(
    root: &Path,
    p: &Profile,
    mcuboot: &Path,
    hal: &Path,
    verify: &Path,
    out: &Path,
    rom: Option<(&Path, &Path)>,
) -> Result<()> {
    use crate::{hash, json::json, output, run, source, write_json};
    use std::{fs, process::Command};
    application_config(p)?;
    let protected = protected(p)?;
    if protected != rom.is_some() {
        return Err(
            "protected loader builds require both ROM signing key and independent ROM root".into(),
        );
    }
    if out.exists() || out.symlink_metadata().is_ok() {
        return Err("loader output must be a fresh directory".into());
    }
    let public = public_root(verify, false)?;
    bootloader_config(p, &out.join("root.pem"))?;
    if let Some((key, root)) = rom {
        if !key.is_file() {
            return Err("missing ROM signing key".into());
        }
        public_root(root, true)?;
    }
    fs::create_dir_all(out).map_err(|e| e.to_string())?;
    source::archive_revision(mcuboot, MCUBOOT_REVISION, &out.join("mcuboot"))?;
    source::archive_revision(hal, HAL_REVISION, &out.join("hal"))?;
    let mut patches = vec![];
    let mut selected = vec![];
    if protected {
        selected.extend([
            (
                "mcuboot",
                "mcuboot",
                "0002-require-provisioned-protection.patch",
            ),
            (
                "mcuboot",
                "mcuboot",
                "0003-encrypted-write-once-flash.patch",
            ),
            (
                "esp-hal-3rdparty",
                "hal",
                "0004-forbid-efuse-programming.patch",
            ),
        ]);
    }
    selected.extend([
        ("mcuboot", "mcuboot", "0006-espressif-image-class.patch"),
        ("mcuboot", "mcuboot", "0007-espressif-watchdog-budget.patch"),
    ]);
    for (component, folder, name) in selected {
        let patch = root
            .join("upstream")
            .join(component)
            .join("patches")
            .join(name);
        source::patch(&out.join(folder), &patch)?;
        patches.push(json!({"component":component,"patch":name,"sha256":hash(&patch)?}));
    }
    fs::write(out.join("root.pem"), public.pem).map_err(|e| e.to_string())?;
    fs::write(
        out.join("bootloader.conf"),
        bootloader_config(p, &out.join("root.pem"))?,
    )
    .map_err(|e| e.to_string())?;
    run(Command::new("cmake")
        .arg("-S")
        .arg(out.join("mcuboot/boot/espressif"))
        .arg("-B")
        .arg(out.join("build"))
        .args(["-G", "Ninja", "-DMCUBOOT_TARGET=esp32s3"])
        .arg(format!(
            "-DMCUBOOT_CONFIG_FILE={}",
            out.join("bootloader.conf").display()
        ))
        .arg(format!("-DESP_HAL_PATH={}", out.join("hal").display()))
        .args([
            "-DCONFIG_ESP_FLASH_SIZE=16MB",
            "-DESP_FLASH_MODE=dio",
            "-DESP_FLASH_FREQ=40m",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        ]))?;
    run(Command::new("cmake")
        .arg("--build")
        .arg(out.join("build"))
        .args(["--parallel", "2"]))?;
    let mut binary = out.join("build/mcuboot_esp32s3.bin");
    if !(1..=0xf000).contains(&binary.metadata().map_err(|e| e.to_string())?.len()) {
        return Err("bootloader exceeds reserved region".into());
    }
    let mut metadata = json!({"mcuboot":MCUBOOT_REVISION,"esp-hal-3rdparty":HAL_REVISION,"platform":p.name(),"layout":p.data["mcuboot"],"image_class":p.data["image-class"],"boot_watchdog_ms":watchdog_timeout(p)?,"application_signature":"ecdsa-p256-sha256","public_root_sha256":hash(&out.join("root.pem"))?,"hardware_secure_boot":protected,"flash_encryption":protected,"patches":patches});
    if let Some((key, root)) = rom {
        let cache = crate::read(&out.join("build/CMakeCache.txt"))?;
        let nm = cache
            .lines()
            .find_map(|l| l.strip_prefix("CMAKE_NM:FILEPATH="))
            .ok_or("missing native symbol tool")?;
        let symbols = output(
            Command::new(nm)
                .arg("--defined-only")
                .arg(out.join("build/mcuboot_esp32s3.elf")),
        )?;
        if symbols
            .lines()
            .any(|l| l.split_whitespace().next_back() == Some("efuse_hal_program"))
        {
            return Err("protected loader contains physical eFuse programmer".into());
        }
        let published = out.join("bootloader.signed.bin");
        let fingerprint = sign_rom(&binary, &published, key, root)?;
        binary = published;
        for (key, value) in [
            ("rom_signature", json!("rsa-3072-pss-sha256")),
            ("rom_root_pem_sha256", json!(fingerprint)),
            ("hardware_qualified", json!(false)),
            ("requires_factory_provisioning", json!(true)),
            ("efuse_writes_forbidden", json!(true)),
            ("encrypted_write_once", json!(true)),
        ] {
            metadata[key] = value;
        }
    }
    metadata["bootloader_sha256"] = json!(hash(&binary)?);
    metadata["bootloader_file"] = json!(binary.strip_prefix(out).map_err(|e| e.to_string())?);
    write_json(&out.join("bootloader.json"), &metadata)
}

pub fn sign_rom(input: &Path, output: &Path, key: &Path, root: &Path) -> Result<String> {
    use std::{fs, process::Command};
    let public = public_root(root, true)?;
    let parent = output
        .parent()
        .ok_or("ROM output needs a parent")?
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let published = parent.join(output.file_name().ok_or("ROM output needs a name")?);
    for path in [input, key, root] {
        if !path.is_file() || path.canonicalize().map_err(|e| e.to_string())? == published {
            return Err("ROM output must not replace an input or key".into());
        }
    }
    if !crate::output(Command::new("esptool.py").arg("version"))?
        .lines()
        .any(|line| line.trim() == "4.8.1")
    {
        return Err("ROM signing requires esptool 4.8.1".into());
    }
    let staging = crate::TempDir::new(&parent)?;
    let expected = staging.0.join("root.pem");
    fs::write(&expected, public.pem).map_err(|e| e.to_string())?;
    let candidate = staging.0.join("bootloader.bin");
    crate::output(
        Command::new("espsecure.py")
            .args(["sign_data", "--version", "2", "--keyfile"])
            .arg(key)
            .arg("--output")
            .arg(&candidate)
            .arg(input),
    )?;
    crate::output(
        Command::new("espsecure.py")
            .args(["verify_signature", "--version", "2", "--keyfile"])
            .arg(expected)
            .arg(&candidate),
    )?;
    if !(1..=0xf000).contains(&candidate.metadata().map_err(|e| e.to_string())?.len()) {
        return Err("ROM signature exceeds reserved bootloader region".into());
    }
    fs::rename(candidate, published).map_err(|e| e.to_string())?;
    Ok(public.source_sha256)
}
